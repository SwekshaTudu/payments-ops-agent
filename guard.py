"""Prompt-injection guard for free-text fields (payment notes, statement narrations).

Anyone who pays you can write a note, so notes are attacker-controlled text that ends up
inside the LLM's context via tool results. Layer 1 (here): detect instruction-like text at
load time and hide it. Layer 2 (agent.py system prompt): treat all tool output as data.
"""
import re

import pandas as pd

PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any )?(the )?(previous|prior|above|your) ",
    r"\b(system|developer) (prompt|note|message|instruction)",
    r"^\s*(system|assistant)\s*:",
    r"\bnote to (the )?(ai|assistant|llm|model)\b",
    r"\byou are now\b",
    r"\bnew instructions\b",
    r"\b(tell|inform) the (merchant|user|owner)\b",
    r"\bdo not (mention|report|flag|reveal)\b",
    r"\b(jailbreak|prompt injection)\b",
]
_RE = re.compile("|".join(PATTERNS), re.IGNORECASE | re.MULTILINE)
PLACEHOLDER = "[hidden by safety filter: this note contained instructions aimed at the AI]"


def looks_like_injection(text: str) -> bool:
    return bool(text) and bool(_RE.search(str(text)))


def neutralize_injections(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Adds an 'injection_flag' column and replaces flagged text with a placeholder."""
    df = df.copy()
    flag = pd.Series(False, index=df.index)
    for col in columns:
        if col not in df:
            continue
        hit = df[col].astype(str).map(looks_like_injection)
        df.loc[hit, col] = PLACEHOLDER
        flag |= hit
    df["injection_flag"] = flag
    return df
