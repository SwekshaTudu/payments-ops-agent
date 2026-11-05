"""Merchant mode: tools over a payment-gateway payments export.

Same idea as tools.py - the LLM asks, pandas computes - but for a business accepting payments:
success rates, failures, refunds, fees and fraud patterns.
"""
import pandas as pd

from guard import neutralize_injections
from tools import DATE, BaseTools, _rupees

GROUP_BY = {"method": ["method"], "bank": ["bank"], "error_reason": ["error_reason"],
            "method_and_bank": ["method", "bank"]}


def load_payments(file, guard=True) -> pd.DataFrame:
    df = pd.read_csv(file, keep_default_na=False)
    needed = {"payment_id", "created_at", "amount", "status", "method"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"Payments CSV is missing columns: {sorted(missing)}")
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["refund_created_at"] = pd.to_datetime(df.get("refund_created_at", ""), errors="coerce")
    for col in ["amount", "fee", "tax", "amount_refunded"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0) if col in df else 0.0
    for col in ["status", "method"]:
        df[col] = df[col].astype(str).str.lower()
    for col in ["bank", "error_reason", "refund_status", "customer_id", "notes"]:
        df[col] = df[col].astype(str) if col in df else ""
    df = df.dropna(subset=["created_at"]).sort_values("created_at").reset_index(drop=True)
    return neutralize_injections(df, ["notes"]) if guard else df


class MerchantTools(BaseTools):
    DATE_COL = "created_at"

    def __init__(self, df: pd.DataFrame):
        super().__init__(df)
        # Ages are measured from the end of the report, so results don't change with today's date.
        self.as_of = df["created_at"].max().normalize() + pd.Timedelta(days=1)

    def _range(self, start_date=None, end_date=None):
        d = self.df
        if start_date:
            d = d[d["created_at"] >= pd.to_datetime(start_date)]
        if end_date:
            d = d[d["created_at"] < pd.to_datetime(end_date) + pd.Timedelta(days=1)]
        return d

    @staticmethod
    def _paid(d):
        """Payments where money actually moved (refunded ones were captured first)."""
        return d[d["status"].isin(["captured", "refunded"])]

    # ---------- tools ----------
    def get_payments_overview(self, start_date=None, end_date=None):
        d = self._range(start_date, end_date)
        paid = self._paid(d)
        gross = paid["amount"].sum()
        fees, tax = paid["fee"].sum(), paid["tax"].sum()
        refunds = d["amount_refunded"].sum()
        return {
            "period": [str(d["created_at"].min().date()), str(d["created_at"].max().date())],
            "payment_attempts": len(d),
            "successful_payments": len(paid),
            "failed_payments": int((d["status"] == "failed").sum()),
            "success_rate_pct": round(100 * len(paid) / max(len(d), 1), 2),
            "gross_sales": _rupees(gross),
            "fees": _rupees(fees),
            "tax_on_fees": _rupees(tax),
            "refunds": _rupees(refunds),
            "net_settlement_estimate": _rupees(gross - fees - tax - refunds),
            "flagged_injection_notes": int(d.get("injection_flag", pd.Series(dtype=bool)).sum()),
        }

    def failure_breakdown(self, group_by="method", start_date=None, end_date=None):
        if group_by not in GROUP_BY:
            return {"error": f"group_by must be one of {list(GROUP_BY)}"}
        d = self._range(start_date, end_date)
        d = d.assign(failed=d["status"] == "failed")
        if group_by == "error_reason":
            g = d[d["failed"]].groupby("error_reason").size().sort_values(ascending=False)
            return [{"error_reason": k, "failures": int(v)} for k, v in g.items()]
        g = d.groupby(GROUP_BY[group_by])["failed"].agg(["count", "sum"])
        g["rate"] = 100 * g["sum"] / g["count"]
        out = []
        for key, r in g.sort_values("rate", ascending=False).iterrows():
            key = key if isinstance(key, tuple) else (key,)
            row = dict(zip(GROUP_BY[group_by], key))
            row.update(attempts=int(r["count"]), failed=int(r["sum"]), failure_rate_pct=round(float(r["rate"]), 1))
            out.append(row)
        return out

    def daily_success_rate(self, method=None, bank=None):
        d = self.df
        if method:
            d = d[d["method"] == method.lower()]
        if bank:
            d = d[d["bank"].str.lower() == bank.lower()]
        g = d.groupby(d["created_at"].dt.date).agg(
            attempts=("status", "size"), ok=("status", lambda s: s.isin(["captured", "refunded"]).sum()))
        g["rate"] = 100 * g["ok"] / g["attempts"]
        cutoff = g["rate"].mean() - 2 * g["rate"].std()
        days = [{"date": str(k), "attempts": int(r["attempts"]), "success_rate_pct": round(float(r["rate"]), 1),
                 "bad_day": bool(r["rate"] < cutoff)} for k, r in g.iterrows()]
        return {"average_success_rate_pct": round(float(g["rate"].mean()), 1), "bad_day_threshold_pct": round(float(cutoff), 1),
                "bad_days": [x for x in days if x["bad_day"]], "days": days}

    def detect_outages(self, max_success_pct=50, min_attempts=4):
        """Scan every method+bank pair in one pass for days where success collapsed vs. its normal rate.

        Added after evals showed the agent making ~19 daily_success_rate/failure_breakdown calls
        to hunt for an outage one bank at a time.
        """
        fraudsters = {f["customer_id"] for f in self.detect_fraud_patterns() if "customer_id" in f}
        d = self.df[~self.df["customer_id"].isin(fraudsters)]  # an attack is not an outage
        d = d.assign(ok=d["status"].isin(["captured", "refunded"]), day=d["created_at"].dt.date)
        outages = []
        for (method, bank), g in d[d["bank"] != ""].groupby(["method", "bank"]):
            daily = g.groupby("day")["ok"].agg(["size", "mean"])
            bad = daily[(daily["size"] >= int(min_attempts)) & (100 * daily["mean"] <= float(max_success_pct))]
            if bad.empty:
                continue
            normal = g[~g["day"].isin(bad.index)]["ok"].mean()
            # Merge consecutive bad days into one window.
            windows, start, prev = [], None, None
            for day in sorted(bad.index):
                if prev is None or (day - prev).days > 1:
                    if start:
                        windows.append((start, prev))
                    start = day
                prev = day
            windows.append((start, prev))
            for s, e in windows:
                w = g[(g["day"] >= s) & (g["day"] <= e)]
                outages.append({
                    "method": method, "bank": bank, "from": str(s), "to": str(e),
                    "attempts": len(w), "failed": int((~w["ok"]).sum()),
                    "success_rate_pct": round(float(100 * w["ok"].mean()), 1),
                    "normal_success_rate_pct": round(float(100 * normal), 1),
                    "top_error": w.loc[~w["ok"], "error_reason"].mode().iloc[0] if (~w["ok"]).any() else "",
                })
        return sorted(outages, key=lambda o: o["success_rate_pct"]) or [{"note": "No outages found."}]

    def refund_tracker(self, min_days_pending=7):
        r = self.df[self.df["amount_refunded"] > 0].copy()
        r["age_days"] = (self.as_of - r["refund_created_at"]).dt.days
        pending = r[(r["refund_status"] == "pending") & (r["age_days"] >= int(min_days_pending))]
        return {
            "as_of": str(self.as_of.date()),
            "total_refunds": len(r),
            "total_refunded_amount": _rupees(r["amount_refunded"].sum()),
            "stuck_refunds": len(pending),
            "stuck_amount": _rupees(pending["amount_refunded"].sum()),
            "stuck": [{"payment_id": x.payment_id, "amount": _rupees(x.amount_refunded), "customer_id": x.customer_id,
                       "refund_requested": str(x.refund_created_at.date()), "days_pending": int(x.age_days)}
                      for x in pending.sort_values("age_days", ascending=False).itertuples()],
        }

    def fee_summary(self):
        paid = self._paid(self.df)
        g = paid.groupby("method").agg(sales=("amount", "sum"), fees=("fee", "sum"), tax=("tax", "sum"),
                                       payments=("amount", "size"))
        rows = [{"method": m, "payments": int(r["payments"]), "sales": _rupees(r["sales"]),
                 "fees_plus_tax": _rupees(r["fees"] + r["tax"]),
                 "effective_cost_pct": round(float(100 * (r["fees"] + r["tax"]) / max(r["sales"], 1)), 2)}
                for m, r in g.sort_values("fees", ascending=False).iterrows()]
        card = g.loc["card"] if "card" in g.index else None
        upi_rate = (g.loc["upi", "fees"] + g.loc["upi", "tax"]) / g.loc["upi", "sales"] if "upi" in g.index else 0
        savings = None
        if card is not None:
            moved = 0.10 * card["sales"]  # what if 10% of card sales moved to UPI
            card_rate = (card["fees"] + card["tax"]) / card["sales"]
            savings = _rupees(moved * (card_rate - upi_rate))
        return {"by_method": rows, "total_fees_plus_tax": _rupees(g["fees"].sum() + g["tax"].sum()),
                "monthly_saving_if_10pct_of_card_sales_moved_to_upi": savings}

    def detect_fraud_patterns(self, window_minutes=60, min_attempts=10, max_median_amount=50):
        """Card testing: one customer firing many tiny payments in a short window."""
        findings = []
        for cust, g in self.df.groupby("customer_id"):
            g = g.sort_values("created_at")
            times = g["created_at"].tolist()
            j = 0
            for i in range(len(times)):
                while times[i] - times[j] > pd.Timedelta(minutes=int(window_minutes)):
                    j += 1
                if i - j + 1 >= int(min_attempts):
                    burst = g[(g["created_at"] >= times[j]) & (g["created_at"] <= times[j] + pd.Timedelta(minutes=int(window_minutes)))]
                    if burst["amount"].median() <= float(max_median_amount):
                        findings.append({
                            "pattern": "card_testing", "customer_id": cust,
                            "start": str(burst["created_at"].min()), "end": str(burst["created_at"].max()),
                            "attempts": len(burst), "failed": int((burst["status"] == "failed").sum()),
                            "succeeded": int((burst["status"] != "failed").sum()),
                            "median_amount": _rupees(burst["amount"].median()),
                            "methods": sorted(burst["method"].unique().tolist()),
                        })
                        break
        return findings or [{"note": "No fraud patterns found."}]

    def search_payments(self, status=None, method=None, bank=None, customer_id=None, min_amount=None,
                        max_amount=None, start_date=None, end_date=None, limit=20):
        d = self._range(start_date, end_date)
        if status:
            d = d[d["status"] == status.lower()]
        if method:
            d = d[d["method"] == method.lower()]
        if bank:
            d = d[d["bank"].str.lower() == bank.lower()]
        if customer_id:
            d = d[d["customer_id"] == customer_id]
        if min_amount is not None:
            d = d[d["amount"] >= float(min_amount)]
        if max_amount is not None:
            d = d[d["amount"] <= float(max_amount)]
        cols = ["payment_id", "created_at", "amount", "status", "method", "bank", "error_reason", "customer_id", "notes"]
        return {"match_count": len(d), "match_total": _rupees(d["amount"].sum()),
                "payments": d.sort_values("created_at", ascending=False).head(int(limit))[cols]
                    .astype({"created_at": str}).to_dict("records")}

    def create_chart(self, chart_type, data_source, title, bank=None, method=None):
        if data_source == "daily_success":
            rows = self.daily_success_rate(method, bank)["days"]
            x, y = "date", "success_rate_pct"
        elif data_source in ("failures_by_bank", "failures_by_method"):
            rows = self.failure_breakdown(data_source.split("_by_")[1])
            x, y = data_source.split("_by_")[1], "failure_rate_pct"
        elif data_source == "fees_by_method":
            rows = self.fee_summary()["by_method"]
            x, y = "method", "fees_plus_tax"
        else:
            return {"error": f"Unknown data_source {data_source}"}
        self.charts.append({"type": chart_type, "title": title, "x": x, "y": y, "rows": rows})
        return {"status": "chart shown to user", "points": len(rows)}

    DECLARATIONS = [
        {"name": "get_payments_overview",
         "description": "Summary: attempts, success rate, gross sales, fees, refunds, net settlement estimate. Call first if you lack context.",
         "parameters": {"type": "object", "properties": {"start_date": DATE, "end_date": DATE}}},
        {"name": "failure_breakdown",
         "description": "Failure counts and rates grouped by method, bank, method_and_bank, or error_reason.",
         "parameters": {"type": "object", "properties": {
             "group_by": {"type": "string", "enum": list(GROUP_BY)}, "start_date": DATE, "end_date": DATE}}},
        {"name": "detect_outages",
         "description": "Scan ALL payment method + bank combinations at once for outages (days where success collapsed). "
                        "Use this first for any question about outages, downtime or sudden failure spikes.",
         "parameters": {"type": "object", "properties": {
             "max_success_pct": {"type": "number"}, "min_attempts": {"type": "integer"}}}},
        {"name": "daily_success_rate",
         "description": "Success rate per day, with statistically bad days flagged. Filter by method and/or bank to find outages.",
         "parameters": {"type": "object", "properties": {"method": {"type": "string"}, "bank": {"type": "string"}}}},
        {"name": "refund_tracker",
         "description": "Refund totals and refunds stuck in 'pending' for at least min_days_pending days.",
         "parameters": {"type": "object", "properties": {"min_days_pending": {"type": "integer"}}}},
        {"name": "fee_summary",
         "description": "Gateway fees + tax by payment method, effective cost %, and savings if some card sales moved to UPI.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "detect_fraud_patterns",
         "description": "Detect card-testing: one customer making many tiny payment attempts in a short window.",
         "parameters": {"type": "object", "properties": {
             "window_minutes": {"type": "integer"}, "min_attempts": {"type": "integer"},
             "max_median_amount": {"type": "number"}}}},
        {"name": "search_payments",
         "description": "Look up individual payments (including customer notes) by status, method, bank, customer, amount or date.",
         "parameters": {"type": "object", "properties": {
             "status": {"type": "string", "enum": ["captured", "failed", "refunded"]},
             "method": {"type": "string", "enum": ["upi", "card", "netbanking", "wallet"]},
             "bank": {"type": "string"}, "customer_id": {"type": "string"},
             "min_amount": {"type": "number"}, "max_amount": {"type": "number"},
             "start_date": DATE, "end_date": DATE, "limit": {"type": "integer"}}}},
        {"name": "create_chart",
         "description": "Show a chart to the user when a visual helps.",
         "parameters": {"type": "object", "properties": {
             "chart_type": {"type": "string", "enum": ["bar", "pie", "line"]},
             "data_source": {"type": "string", "enum": ["daily_success", "failures_by_bank", "failures_by_method", "fees_by_method"]},
             "title": {"type": "string"},
             "bank": {"type": "string", "description": "Optional bank filter for daily_success"},
             "method": {"type": "string", "description": "Optional method filter for daily_success"}},
             "required": ["chart_type", "data_source", "title"]}},
    ]
