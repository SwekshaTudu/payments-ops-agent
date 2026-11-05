"""Load bank / UPI statement CSVs into one clean DataFrame.

Output columns: date (datetime), description (str), merchant (str),
amount (float, always positive), direction ("debit" | "credit").
"""
import re

import pandas as pd

from guard import neutralize_injections

DATE_COLS = ["date", "txn date", "transaction date", "value date", "posting date"]
DESC_COLS = ["description", "narration", "remarks", "particulars", "details", "merchant"]
AMOUNT_COLS = ["amount", "txn amount", "transaction amount"]
DEBIT_COLS = ["debit", "withdrawal", "withdrawal amt", "dr"]
CREDIT_COLS = ["credit", "deposit", "deposit amt", "cr"]
TYPE_COLS = ["type", "dr/cr", "txn type", "direction"]


def _find(cols, candidates):
    for c in candidates:
        if c in cols:
            return cols[c]
    return None


def _to_number(series):
    cleaned = series.astype(str).str.replace(r"[₹,\s]|INR|Rs\.?", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def clean_merchant(description: str) -> str:
    """'UPI/DR/412345/SWIGGY/okaxis' -> 'Swiggy'."""
    text = str(description)
    parts = [p for p in re.split(r"[/|\-]", text) if p.strip()]
    noise = {"upi", "dr", "cr", "pos", "neft", "imps", "ach", "nach", "okaxis", "oksbi",
             "okhdfcbank", "okicici", "ybl", "paytm", "payment", "txn"}
    words = [p.strip() for p in parts if p.strip().lower() not in noise and not p.strip().isdigit()]
    name = words[0] if words else text
    name = re.sub(r"\d{4,}", "", name).strip()
    return name.title()[:40] or "Unknown"


def load_statement(file, guard=True) -> pd.DataFrame:
    raw = pd.read_csv(file)
    cols = {c.strip().lower(): c for c in raw.columns}

    date_col = _find(cols, DATE_COLS)
    desc_col = _find(cols, DESC_COLS)
    if not date_col or not desc_col:
        raise ValueError(f"Could not find date/description columns in: {list(raw.columns)}")

    df = pd.DataFrame()
    df["date"] = pd.to_datetime(raw[date_col], errors="coerce", dayfirst=True)
    df["description"] = raw[desc_col].astype(str)

    amount_col = _find(cols, AMOUNT_COLS)
    debit_col, credit_col = _find(cols, DEBIT_COLS), _find(cols, CREDIT_COLS)
    type_col = _find(cols, TYPE_COLS)

    if debit_col and credit_col:
        debit = _to_number(raw[debit_col]).fillna(0)
        credit = _to_number(raw[credit_col]).fillna(0)
        df["amount"] = debit.where(debit > 0, credit)
        df["direction"] = ["debit" if d > 0 else "credit" for d in debit]
    elif amount_col:
        amount = _to_number(raw[amount_col])
        if type_col:
            t = raw[type_col].astype(str).str.lower()
            df["direction"] = ["credit" if ("cr" in x or "credit" in x) else "debit" for x in t]
        else:
            # Signed amounts: negative = money out.
            df["direction"] = ["debit" if a < 0 else "credit" for a in amount]
        df["amount"] = amount.abs()
    else:
        raise ValueError("Could not find amount or debit/credit columns.")

    df = df.dropna(subset=["date", "amount"])
    df["merchant"] = df["description"].map(clean_merchant)
    df = df.sort_values("date").reset_index(drop=True)
    return neutralize_injections(df, ["description", "merchant"]) if guard else df
