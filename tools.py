"""Tools the agent can call. Each returns plain JSON-serializable data.

The LLM never sees the raw statement - it has to call these tools to get numbers,
which keeps answers grounded (no made-up totals) and keeps the prompt small.
"""
import pandas as pd


def _rupees(x) -> float:
    return round(float(x), 2)


class BaseTools:
    """Shared plumbing: holds the data, queued charts, and safe dispatch of tool calls by name."""
    DECLARATIONS: list = []
    DATE_COL = "date"

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.charts = []  # chart specs the UI renders after each agent turn

    def data_period(self):
        dates = self.df[self.DATE_COL]
        return str(dates.min().date()), str(dates.max().date())

    def call(self, name, args):
        if name not in {t["name"] for t in self.DECLARATIONS}:
            return {"error": f"Unknown tool {name}"}
        try:
            return getattr(self, name)(**(args or {}))
        except Exception as e:  # report errors back to the model so it can retry
            return {"error": f"{type(e).__name__}: {e}"}


class ExpenseTools(BaseTools):

    # ---------- helpers ----------
    def _debits(self, start_date=None, end_date=None, category=None):
        d = self.df[self.df["direction"] == "debit"]
        if start_date:
            d = d[d["date"] >= pd.to_datetime(start_date)]
        if end_date:
            d = d[d["date"] <= pd.to_datetime(end_date)]
        if category:
            d = d[d["category"].str.lower() == category.lower()]
        return d

    # ---------- tools ----------
    def get_overview(self):
        df = self.df
        debits = df[df["direction"] == "debit"]
        credits = df[df["direction"] == "credit"]
        months = max(debits["date"].dt.to_period("M").nunique(), 1)
        return {
            "date_range": [str(df["date"].min().date()), str(df["date"].max().date())],
            "transactions": len(df),
            "total_spent": _rupees(debits["amount"].sum()),
            "total_received": _rupees(credits["amount"].sum()),
            "net_savings": _rupees(credits["amount"].sum() - debits["amount"].sum()),
            "avg_monthly_spend": _rupees(debits["amount"].sum() / months),
            "categories": sorted(df["category"].unique().tolist()),
        }

    def spending_by_category(self, start_date=None, end_date=None):
        d = self._debits(start_date, end_date)
        total = d["amount"].sum() or 1
        grouped = d.groupby("category")["amount"].agg(["sum", "count"]).sort_values("sum", ascending=False)
        return [
            {"category": c, "total": _rupees(r["sum"]), "transactions": int(r["count"]),
             "share_pct": round(100 * r["sum"] / total, 1)}
            for c, r in grouped.iterrows()
        ]

    def top_merchants(self, n=10, category=None, start_date=None, end_date=None):
        d = self._debits(start_date, end_date, category)
        grouped = d.groupby("merchant")["amount"].agg(["sum", "count"]).sort_values("sum", ascending=False).head(int(n))
        return [{"merchant": m, "total": _rupees(r["sum"]), "transactions": int(r["count"])}
                for m, r in grouped.iterrows()]

    def monthly_trend(self, category=None):
        d = self._debits(category=category)
        monthly = d.groupby(d["date"].dt.to_period("M"))["amount"].sum()
        rows = [{"month": str(p), "spent": _rupees(v)} for p, v in monthly.items()]
        for i in range(1, len(rows)):
            prev = rows[i - 1]["spent"] or 1
            rows[i]["change_pct"] = round(100 * (rows[i]["spent"] - prev) / prev, 1)
        return rows

    def search_transactions(self, keyword=None, category=None, direction=None, min_amount=None,
                            max_amount=None, start_date=None, end_date=None, limit=20):
        d = self.df
        if keyword:
            d = d[d["description"].str.contains(keyword, case=False, regex=False)]
        if category:
            d = d[d["category"].str.lower() == category.lower()]
        if direction:
            d = d[d["direction"] == direction]
        if min_amount is not None:
            d = d[d["amount"] >= float(min_amount)]
        if max_amount is not None:
            d = d[d["amount"] <= float(max_amount)]
        if start_date:
            d = d[d["date"] >= pd.to_datetime(start_date)]
        if end_date:
            d = d[d["date"] <= pd.to_datetime(end_date)]
        return {
            "match_count": len(d),
            "match_total": _rupees(d["amount"].sum()),
            "transactions": [
                {"date": str(r.date.date()), "merchant": r.merchant, "amount": _rupees(r.amount),
                 "direction": r.direction, "category": r.category}
                for r in d.sort_values("amount", ascending=False).head(int(limit)).itertuples()
            ],
        }

    def find_anomalies(self):
        d = self._debits()
        findings = []

        # 1) Unusually large payments compared to the rest of that category.
        for cat, g in d.groupby("category"):
            if len(g) < 4:
                continue
            median = g["amount"].median()
            threshold = max(3 * median, median + 2 * g["amount"].std(), 2000)  # ignore small-ticket noise
            for r in g[g["amount"] > threshold].itertuples():
                findings.append({"type": "unusually_large", "date": str(r.date.date()), "merchant": r.merchant,
                                 "amount": _rupees(r.amount), "category": cat,
                                 "note": f"{r.amount / median:.1f}x the typical {cat} payment (median ₹{median:.0f})"})

        # 2) Possible duplicate charges: same merchant + same amount within 2 days.
        s = d.sort_values("date")
        for (_, amount), g in s.groupby(["merchant", "amount"]):
            dates = g["date"].tolist()
            for a, b in zip(dates, dates[1:]):
                if (b - a).days <= 2:
                    findings.append({"type": "possible_duplicate", "date": str(b.date()), "merchant": g["merchant"].iloc[0],
                                     "amount": _rupees(amount), "note": f"Same amount also charged on {a.date()}"})
        return findings or [{"note": "No anomalies found."}]

    def find_recurring_payments(self):
        """Merchants charged in 2+ different months with a stable amount - likely subscriptions/EMIs."""
        d = self._debits()
        out = []
        for merchant, g in d.groupby("merchant"):
            months = g["date"].dt.to_period("M").nunique()
            if months < 2:
                continue
            amt = g["amount"]
            if amt.std(ddof=0) <= 0.1 * amt.mean():
                out.append({"merchant": merchant, "typical_amount": _rupees(amt.median()), "months_seen": int(months),
                            "category": g["category"].iloc[0], "first_seen": str(g["date"].min().date()),
                            "yearly_cost": _rupees(amt.median() * 12)})
        return sorted(out, key=lambda x: -x["yearly_cost"]) or [{"note": "No recurring payments found."}]

    def create_chart(self, chart_type, data_source, title, category=None):
        """Builds a chart from a data source and queues it for the UI."""
        if data_source == "category":
            rows = self.spending_by_category()
            x, y = "category", "total"
        elif data_source == "monthly":
            rows = self.monthly_trend(category)
            x, y = "month", "spent"
        elif data_source == "merchants":
            rows = self.top_merchants(10, category)
            x, y = "merchant", "total"
        else:
            return {"error": f"Unknown data_source {data_source}"}
        self.charts.append({"type": chart_type, "title": title, "x": x, "y": y, "rows": rows})
        return {"status": "chart shown to user", "points": len(rows)}


DATE = {"type": "string", "description": "ISO date YYYY-MM-DD"}

TOOL_DECLARATIONS = [
    {"name": "get_overview",
     "description": "High-level summary: date range, totals spent/received, net savings, avg monthly spend, list of categories. Call this first if you lack context.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "spending_by_category",
     "description": "Total spend and share per category, optionally within a date range.",
     "parameters": {"type": "object", "properties": {"start_date": DATE, "end_date": DATE}}},
    {"name": "top_merchants",
     "description": "Merchants with the highest total spend, optionally filtered by category/date.",
     "parameters": {"type": "object", "properties": {
         "n": {"type": "integer"}, "category": {"type": "string"}, "start_date": DATE, "end_date": DATE}}},
    {"name": "monthly_trend",
     "description": "Spend per month with month-over-month % change, optionally for one category.",
     "parameters": {"type": "object", "properties": {"category": {"type": "string"}}}},
    {"name": "search_transactions",
     "description": "Find individual transactions by keyword, category, direction, amount range or dates. Returns count, total, and the largest matches.",
     "parameters": {"type": "object", "properties": {
         "keyword": {"type": "string"}, "category": {"type": "string"},
         "direction": {"type": "string", "enum": ["debit", "credit"]},
         "min_amount": {"type": "number"}, "max_amount": {"type": "number"},
         "start_date": DATE, "end_date": DATE, "limit": {"type": "integer"}}}},
    {"name": "find_anomalies",
     "description": "Detect unusually large payments and possible duplicate charges.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "find_recurring_payments",
     "description": "Detect subscriptions / recurring payments and their yearly cost.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "create_chart",
     "description": "Show a chart to the user. Use when a visual helps.",
     "parameters": {"type": "object", "properties": {
         "chart_type": {"type": "string", "enum": ["bar", "pie", "line"]},
         "data_source": {"type": "string", "enum": ["category", "monthly", "merchants"]},
         "title": {"type": "string"},
         "category": {"type": "string", "description": "Optional category filter for monthly/merchants"}},
         "required": ["chart_type", "data_source", "title"]}},
]

ExpenseTools.DECLARATIONS = TOOL_DECLARATIONS
