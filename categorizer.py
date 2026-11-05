"""Two-stage categorization: fast keyword rules first, then the LLM for whatever is left."""
import json

CATEGORIES = [
    "Food & Dining", "Groceries", "Transport", "Shopping", "Bills & Utilities",
    "Entertainment", "Subscriptions", "Rent", "Health", "Travel", "Education",
    "Transfers", "Income", "Other",
]

# Order matters: first match wins, so "Amazon Prime" hits Subscriptions before Shopping.
RULES = {
    "Subscriptions": ["netflix", "spotify", "prime", "hotstar", "youtube", "chatgpt", "claude", "notion", "icloud", "google one"],
    "Food & Dining": ["swiggy", "zomato", "dominos", "mcdonald", "kfc", "starbucks", "cafe", "restaurant", "eatsure"],
    "Groceries": ["bigbasket", "blinkit", "zepto", "dmart", "instamart", "grocer", "kirana", "jiomart"],
    "Transport": ["uber", "ola", "rapido", "metro", "irctc", "petrol", "fuel", "hpcl", "bpcl", "iocl", "fastag"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "decathlon"],
    "Bills & Utilities": ["electricity", "bescom", "tata power", "jio", "airtel", "vi ", "broadband", "gas", "water bill", "act fibernet"],
    "Entertainment": ["bookmyshow", "pvr", "inox", "steam", "playstation"],
    "Rent": ["rent", "nobroker", "landlord"],
    "Health": ["pharmeasy", "apollo", "1mg", "netmeds", "hospital", "clinic", "cult.fit", "gym"],
    "Travel": ["makemytrip", "goibibo", "indigo", "airindia", "oyo", "airbnb", "cleartrip"],
    "Education": ["udemy", "coursera", "byju", "unacademy", "college", "fees"],
}


def rule_category(description: str, direction: str) -> str | None:
    text = description.lower()
    if direction == "credit":
        return "Income" if any(k in text for k in ["salary", "interest", "refund", "cashback"]) else "Transfers"
    for category, keywords in RULES.items():
        if any(k in text for k in keywords):
            return category
    return None


def llm_categorize(client, model: str, descriptions: list[str]) -> dict[str, str]:
    """Ask the LLM to label merchants the rules couldn't. Returns {description: category}."""
    if not descriptions:
        return {}
    prompt = (
        "Categorize each Indian bank/UPI transaction description into exactly one of these categories: "
        f"{CATEGORIES}.\nReturn a JSON object mapping each description string to its category.\n\n"
        + "\n".join(f"- {d}" for d in descriptions)
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0},
    )
    try:
        result = json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return {d: c if c in CATEGORIES else "Other" for d, c in result.items()}


def categorize(df, client=None, model=None):
    """Adds 'category' and 'category_source' (rule / llm / default) columns."""
    df = df.copy()
    cats = [rule_category(d, dr) for d, dr in zip(df["description"], df["direction"])]
    df["category"] = cats
    df["category_source"] = ["rule" if c else None for c in cats]

    unknown = df.loc[df["category"].isna(), "description"].unique().tolist()
    if unknown and client is not None:
        try:
            mapping = llm_categorize(client, model, unknown[:150])
        except Exception:
            mapping = {}
        mask = df["category"].isna() & df["description"].isin(mapping)
        df.loc[mask, "category"] = df.loc[mask, "description"].map(mapping)
        df.loc[mask, "category_source"] = "llm"

    df["category_source"] = df["category_source"].fillna("default")
    df["category"] = df["category"].fillna("Other")
    return df
