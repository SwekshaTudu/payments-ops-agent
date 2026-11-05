import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from agent import DEFAULT_MODEL, Agent, QuotaExhausted, make_client
from categorizer import categorize
from data_loader import load_statement
from merchant_tools import MerchantTools, load_payments
from tools import ExpenseTools

load_dotenv()
st.set_page_config(page_title="Payments Analyst Agent", page_icon="💸", layout="wide")
HERE = Path(__file__).parent

MODES = {
    "merchant": {
        "label": "🏪 Merchant (payments)",
        "sample": HERE / "sample_payments.csv",
        "upload_label": "Payments export (.csv)",
        "title": "🏪 Payments Ops Agent",
        "caption": "An AI agent that reads a merchant's payment-gateway export and explains failures, outages, "
                   "stuck refunds, fees and fraud - by calling analysis tools.",
        "suggestions": [
            "Give me a health check of my payments this month. What needs my attention?",
            "Were there any outages? Which bank or method was affected and when?",
            "Is anyone attacking my checkout? Check for fraud.",
            "Which refunds are stuck, and how much money is involved?",
        ],
        "placeholder": "Ask about your payments...",
    },
    "personal": {
        "label": "👤 Personal (spending)",
        "sample": HERE / "sample_statement.csv",
        "upload_label": "Bank / UPI statement (.csv)",
        "title": "💸 Expense Analyst Agent",
        "caption": "An AI agent that reads your bank/UPI statement, categorizes spending, catches anomalies, "
                   "and answers questions by calling analysis tools.",
        "suggestions": [
            "Where is most of my money going, and what should I cut?",
            "Find anything suspicious or unusual in my statement.",
            "What subscriptions am I paying for and what do they cost per year?",
            "How did my food delivery spending change month to month? Show a chart.",
        ],
        "placeholder": "Ask about your spending...",
    },
}


def inr(x):
    """Compact Indian format: 235537 -> ₹2.36L, 78512 -> ₹78.5K."""
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:.2f}L"
    return f"₹{x / 1e3:.1f}K" if abs(x) >= 1e3 else f"₹{x:.0f}"


def render_chart(spec):
    df = pd.DataFrame(spec["rows"])
    if df.empty:
        return
    if spec["type"] == "pie":
        fig = px.pie(df, names=spec["x"], values=spec["y"], title=spec["title"], hole=0.4)
    elif spec["type"] == "line":
        fig = px.line(df, x=spec["x"], y=spec["y"], title=spec["title"], markers=True)
    else:
        fig = px.bar(df, x=spec["x"], y=spec["y"], title=spec["title"])
    st.plotly_chart(fig, use_container_width=True)


def render_trace(trace):
    tool_steps = [t for t in trace if t["type"] == "tool"]
    if not tool_steps:
        return
    with st.expander(f"🧠 Agent reasoning: {len(tool_steps)} tool call(s)"):
        for t in tool_steps:
            st.markdown(f"**Step {t['step']} → `{t['tool']}`** `{json.dumps(t['args'])}`")
            st.code(t["result_preview"], language="json")


# ---------- sidebar ----------
with st.sidebar:
    st.header("⚙️ Setup")
    try:  # Streamlit Cloud secrets, then .env / environment
        default_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        default_key = ""
    api_key = st.text_input("Gemini API key", value=default_key or os.getenv("GEMINI_API_KEY", ""), type="password",
                            help="Free key: https://aistudio.google.com/apikey")
    model = st.text_input("Model", value=DEFAULT_MODEL)
    mode = st.radio("Mode", list(MODES), format_func=lambda m: MODES[m]["label"])
    cfg = MODES[mode]
    source = st.radio("Data", ["Use sample data", "Upload my CSV"])
    upload = st.file_uploader(cfg["upload_label"], type="csv") if source == "Upload my CSV" else None
    guard_on = st.toggle("🛡️ Prompt-injection protection", value=True,
                         help="Turn off to demo the attack: a customer note tries to hijack the agent.")
    st.caption("Your CSV stays in this session. The LLM only sees tool results "
               "(and unknown merchant names for categorization) - never the full file.")
    if st.button("🔄 Reset conversation"):
        st.session_state.pop("agent", None)
        st.session_state["messages"] = []

file = upload if upload is not None else (cfg["sample"] if source == "Use sample data" else None)

st.title(cfg["title"])
st.caption(cfg["caption"])

if file is None:
    st.info("Upload a CSV in the sidebar to begin.")
    st.stop()
if not api_key:
    st.warning("Add your Gemini API key in the sidebar to start chatting with the agent.")

# ---------- load (once per mode/file/key/guard) ----------
data_id = f"{mode}:{getattr(file, 'name', str(file))}:{getattr(file, 'size', '')}:{bool(api_key)}:{model}:{guard_on}"
if st.session_state.get("data_id") != data_id:
    client = make_client(api_key) if api_key else None
    with st.spinner("Reading data..."):
        try:
            if mode == "merchant":
                df = load_payments(file, guard=guard_on)
            else:
                df = categorize(load_statement(file, guard=guard_on), client, model)
        except ValueError as e:
            st.error(str(e))
            st.stop()
    st.session_state.update(data_id=data_id, df=df, messages=[], client=client)
    st.session_state.pop("agent", None)

df = st.session_state["df"]
tools = MerchantTools(df) if mode == "merchant" else ExpenseTools(df)

flagged = int(df["injection_flag"].sum()) if "injection_flag" in df else 0
if flagged:
    st.error(f"🛡️ {flagged} record(s) contained text that tried to give orders to the AI (prompt injection). "
             "They were hidden from the agent. See the data tab (injection_flag = True).")
elif not guard_on:
    st.warning("Prompt-injection protection is OFF - the agent sees raw customer notes.")

# ---------- dashboard ----------
c1, c2, c3, c4 = st.columns(4)
if mode == "merchant":
    ov = tools.get_payments_overview()
    refunds = tools.refund_tracker()
    c1.metric("Gross sales", inr(ov["gross_sales"]))
    c2.metric("Success rate", f"{ov['success_rate_pct']}%", f"{ov['failed_payments']} failed", delta_color="off")
    c3.metric("Fees + tax", inr(ov["fees"] + ov["tax_on_fees"]))
    c4.metric("Stuck refunds", refunds["stuck_refunds"], inr(refunds["stuck_amount"]), delta_color="inverse")
else:
    ov = tools.get_overview()
    c1.metric("Total spent", inr(ov["total_spent"]))
    c2.metric("Total received", inr(ov["total_received"]))
    c3.metric("Net savings", inr(ov["net_savings"]))
    c4.metric("Avg monthly spend", inr(ov["avg_monthly_spend"]))

tab_chat, tab_overview, tab_data = st.tabs(["💬 Ask the agent", "📊 Overview", "🧾 Data"])

with tab_overview:
    left, right = st.columns(2)
    if mode == "merchant":
        with left:
            render_chart({"type": "line", "title": "Daily payment success rate (%)", "x": "date",
                          "y": "success_rate_pct", "rows": tools.daily_success_rate()["days"]})
        with right:
            render_chart({"type": "bar", "title": "Failure rate by bank (%)", "x": "bank", "y": "failure_rate_pct",
                          "rows": tools.failure_breakdown("bank")})
    else:
        with left:
            render_chart({"type": "pie", "title": "Spending by category", "x": "category", "y": "total",
                          "rows": tools.spending_by_category()})
        with right:
            render_chart({"type": "bar", "title": "Monthly spend", "x": "month", "y": "spent",
                          "rows": tools.monthly_trend()})

with tab_data:
    if mode == "personal":
        counts = df["category_source"].value_counts().to_dict()
        st.caption(f"Categorized by keyword rules: {counts.get('rule', 0)} · by LLM: {counts.get('llm', 0)} · "
                   f"uncategorized: {counts.get('default', 0)}")
    st.dataframe(df, use_container_width=True, hide_index=True)

with tab_chat:
    messages = st.session_state.setdefault("messages", [])
    for m in messages:
        with st.chat_message(m["role"]):
            if m["role"] == "assistant":
                render_trace(m.get("trace", []))
            st.markdown(m["content"])
            for spec in m.get("charts", []):
                render_chart(spec)

    if not messages:
        st.markdown("**Try asking:**")
        cols = st.columns(2)
        for i, s in enumerate(cfg["suggestions"]):
            if cols[i % 2].button(s, use_container_width=True):
                st.session_state["pending"] = s
                st.rerun()

    question = st.chat_input(cfg["placeholder"], disabled=not api_key) or st.session_state.pop("pending", None)
    if question and api_key:
        messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        if "agent" not in st.session_state:
            st.session_state["agent"] = Agent(st.session_state["client"] or make_client(api_key), tools,
                                              mode=mode, model=model, hardened=guard_on)
        agent = st.session_state["agent"]
        agent.tools.charts = []

        with st.chat_message("assistant"):
            with st.status("Agent is working...", expanded=True) as status:
                def on_step(e):
                    st.write(f"🔧 Step {e['step']}: calling `{e['tool']}` with `{json.dumps(e['args'])}`")
                try:
                    answer, trace = agent.run(question, on_step=on_step)
                    status.update(label="Done", state="complete", expanded=False)
                    if agent.model != model:
                        answer += f"\n\n<sub>Answered by backup model `{agent.model}` (daily quota on `{model}` was used up).</sub>"
                except QuotaExhausted:
                    answer, trace = ("⏳ This demo's free daily AI quota is used up. Paste your own free Gemini key "
                                     "in the sidebar (get one in a minute at https://aistudio.google.com/apikey) "
                                     "to keep going. Your key stays in your browser session only."), []
                    status.update(label="Quota used up", state="error")
                except Exception as e:
                    answer, trace = f"⚠️ Error talking to the model: {e}", []
                    status.update(label="Error", state="error")
            render_trace(trace)
            st.markdown(answer)
            charts = list(agent.tools.charts)
            for spec in charts:
                render_chart(spec)
        messages.append({"role": "assistant", "content": answer, "trace": trace, "charts": charts})
