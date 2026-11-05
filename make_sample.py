"""Generate a realistic fake 3-month UPI/bank statement for demos (no real data)."""
import random
from datetime import date, timedelta

import pandas as pd

random.seed(7)
START = date(2026, 8, 1)
DAYS = 92
rows = []


def add(d, desc, amount, kind="DR"):
    rows.append({"Date": d.strftime("%d/%m/%Y"), "Narration": desc, "Amount": round(amount, 2), "Type": kind})


def upi(name, handle="okaxis"):
    return f"UPI/{random.randint(400000, 499999)}/{name}/{handle}"


for i in range(DAYS):
    d = START + timedelta(days=i)
    if d.day == 1:
        add(d, "NEFT/SALARY/ACME TECHNOLOGIES PVT LTD", 85000, "CR")
        add(d, upi("RENT LANDLORD SHARMA"), 22000)
    if d.day == 5:
        add(d, upi("NETFLIX", "ybl"), 649)
        add(d, upi("SPOTIFY", "ybl"), 119)
        add(d, upi("JIO PREPAID RECHARGE"), 299)
    if d.day == 12:
        add(d, upi("BESCOM ELECTRICITY"), random.uniform(1100, 1900))
    if d.day == 15:
        add(d, "ACH/CULT.FIT GYM MEMBERSHIP", 1499)
    # A new subscription that started in September.
    if d.day == 20 and d.month >= 9:
        add(d, upi("CHATGPT PLUS", "ybl"), 1999)

    if random.random() < 0.65:
        add(d, upi(random.choice(["SWIGGY", "ZOMATO", "SWIGGY", "DOMINOS"])), random.uniform(180, 750))
    if random.random() < 0.35:
        add(d, upi(random.choice(["UBER INDIA", "OLA CABS", "RAPIDO"])), random.uniform(90, 480))
    if random.random() < 0.25:
        add(d, upi(random.choice(["BLINKIT", "ZEPTO", "BIGBASKET"])), random.uniform(250, 1800))
    if random.random() < 0.12:
        add(d, upi(random.choice(["AMAZON PAY", "FLIPKART", "MYNTRA"])), random.uniform(400, 3500))
    if random.random() < 0.15:
        # Local shops with no obvious keyword - the LLM categorizes these.
        add(d, upi(random.choice(["RAMESH TEA STALL", "SRI BALAJI MEDICALS", "LAKSHMI TAILORS",
                                  "GREEN LEAF VEG MART", "HAPPY PAWS PET CLINIC"])), random.uniform(40, 900))
    if random.random() < 0.08:
        add(d, upi(random.choice(["HP PETROL PUMP", "INDIAN OIL FUEL"])), random.uniform(500, 1500))

# Planted anomalies for the demo.
add(date(2026, 9, 14), upi("CROMA ELECTRONICS"), 42999)
add(date(2026, 10, 9), upi("ZOMATO"), 1240)
add(date(2026, 10, 9), upi("ZOMATO"), 1240)
add(date(2026, 10, 18), upi("BOOKMYSHOW"), 1350)
add(date(2026, 9, 25), "IMPS/REFUND/AMAZON", 1899, "CR")

df = pd.DataFrame(rows)
df["_d"] = pd.to_datetime(df["Date"], dayfirst=True)
df = df.sort_values("_d").drop(columns="_d")
df.to_csv("sample_statement.csv", index=False)
print(f"Wrote sample_statement.csv with {len(df)} transactions")
