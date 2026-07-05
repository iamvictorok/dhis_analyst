"""Content generation node — mode-selective, LLM-powered.

Only generates content for the active output_mode. All narrative generation
requires a configured LLM provider.
"""
from __future__ import annotations

import logging

from backend.config import Settings
from backend.app.agent.renderers.dashboard import render_dashboard
from backend.app.agent.renderers.presentation import render_presentation
from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.generate_content")


async def generate_content(state: AgentState, settings: Settings) -> AgentState:
    """Generate output content for the active mode only."""
    mode = state["output_mode"]

    logger.info(
        "generate_content_start",
        extra={
            "session_id": state.get("session_id"),
            "mode": mode,
            "metrics_count": len(state.get("metrics", [])),
        }
    )

    if mode == "dashboard":
        state["active_chart_configs"] = render_dashboard(state)
    elif mode == "report":
        state["active_report_html"] = await _generate_report(state, settings)
        # Also generate chart configs for embedding in report
        state["active_chart_configs"] = render_dashboard(state)
    elif mode == "presentation":
        # Chart configs needed for slide embedding
        state["active_chart_configs"] = render_dashboard(state)
        state["active_slide_manifest"] = render_presentation(state)
    elif mode == "export":
        pass  # Export uses raw dhis2_data directly
    else:
        # Conversational — optionally generate chart configs for inline reference
        state["active_chart_configs"] = render_dashboard(state)
        state["active_conversational_response"] = await _generate_conversational_response(state, settings)

    logger.info(
        "content_generated",
        extra={
            "session_id": state["session_id"],
            "mode": mode,
            "chart_count": len(state.get("active_chart_configs", [])),
        },
    )
    return state


async def _generate_report(state: AgentState, settings: Settings) -> str:
    """Generate report HTML using the LLM."""
    return await _llm_report(state, settings)


async def _llm_report(state: AgentState, settings: Settings) -> str:
    """Generate a structured report using the LLM."""
    from backend.app.llm import complete

    data = state.get("dhis2_data", {})
    rows = data.get("rows", [])
    headers = data.get("headers", [])
    metric = state["metrics"][0]["label"] if state["metrics"] else "Health Indicator"
    org = state["org_unit"]["label"]
    periods = ", ".join(state["periods"])
    web_context = ""
    for ctx in state.get("web_context", []):
        if not ctx.get("url", "").startswith("local://"):
            web_context += f"\n- {ctx.get('title', '')}: {ctx.get('content', '')[:300]}"

    data_summary = f"Headers: {headers}\nRows ({len(rows)} total):\n"
    for row in rows[:20]:
        data_summary += f"  {row}\n"

    prompt = f"""Write a structured HTML public health report based on this data.

Metric: {metric}
Organisation unit: {org}
Periods: {periods}
Data:
{data_summary}
{f'External context:{web_context}' if web_context else ''}

Structure the report as:
1. <h1> title
2. <h2>Executive Summary</h2> — 2-3 sentence overview
3. <h2>Key Findings</h2> — bullet points with trends, notable values
4. <h2>Data Table</h2> — full HTML table with the data
5. <h2>Trend Analysis</h2> — interpret the numbers, compute % changes
6. <h2>Recommendations</h2> — actionable public health recommendations
7. <h2>Data Sources</h2> — attribution

Return ONLY the HTML body content (no <html>, <head>, <body> tags).
Use semantic HTML. Tables should use <thead> and <tbody>."""

    return await complete(
        [{"role": "system", "content": "You are a public health report writer."}, {"role": "user", "content": prompt}],
        settings,
        temperature=0.4,
    )


async def _generate_conversational_response(state: AgentState, settings: Settings) -> str:
    """Generate conversational response using the LLM."""
    return await _llm_conversational(state, settings)


async def _llm_conversational(state: AgentState, settings: Settings) -> str:
    """Generate a natural conversational response using the LLM."""
    from backend.app.llm import complete

    data = state.get("dhis2_data", {})
    rows = data.get("rows", [])
    headers = data.get("headers", [])

    # --- Casual chitchat path: no data pipeline was run ---
    if not state.get("requires_data") and not rows:
        system_prompt = (
            "You are DHIS2 AI Analyst, an expert public health intelligence assistant. "
            "The user has sent a casual or general message. Respond warmly, briefly, "
            "and ask how you can help with their DHIS2 public health data analysis needs. "
            "Use markdown formatting."
        )
        llm_messages = [{"role": "system", "content": system_prompt}]
        for msg in state.get("messages", []):
            llm_messages.append({"role": msg["role"], "content": msg["content"]})
        return await complete(llm_messages, settings, temperature=0.5)

    # --- Data-backed response path ---
    metrics = state.get("metrics", [])
    metric_label = metrics[0]["label"] if metrics else "Health Indicator"
    org_label = state.get("org_unit", {}).get("label", "National")
    periods = state.get("periods", [])
    period_text = ", ".join(periods)

    # Guard: if data was requested but nothing came back, say so honestly
    if state.get("requires_data") and not rows:
        return (
            f"I was unable to retrieve any data for **{metric_label}** "
            f"in **{org_label}** for the period(s) {period_text}.\n\n"
            f"This may be because:\n"
            f"- The indicator doesn't exist in this DHIS2 instance\n"
            f"- The organisation unit name doesn't match\n"
            f"- No data was reported for that period\n"
            f"- The DHIS2 server was temporarily unreachable\n\n"
            f"Please check the indicator name and try again."
        )

    web_context = ""
    for ctx in state.get("web_context", []):
        if not ctx.get("url", "").startswith("local://"):
            web_context += f"\n- {ctx.get('title', '')}: {ctx.get('content', '')[:300]}"

    data_summary = f"DHIS2 Data for {metric_label} in {org_label} ({period_text}):\nHeaders: {headers}\nRows:\n"
    for row in rows[:50]:
        data_summary += f"  {row}\n"

    system_prompt = (
        "You are an expert public health intelligence assistant. "
        "You analyze DHIS2 data and web context to answer the user's questions clearly, concisely, and accurately. "
        "Provide professional, evidence-based answers using markdown formatting."
    )

    user_query = state["messages"][-1]["content"] if state.get("messages") else ""

    prompt = f"""Based on the following retrieved DHIS2 data and external context, answer the user's question.

Retrieved Data:
{data_summary}

{f'External Context:{web_context}' if web_context else ''}

User Question: {user_query}

Respond in clean markdown. Keep your response professional, focused, and directly addressing their question. 
If they asked a follow-up question, use the data to answer it. Do not include summary statistics if the data is empty.
"""
    
    llm_messages = [{"role": "system", "content": system_prompt}]
    history_messages = state.get("messages", [])[:-1]
    for msg in history_messages:
        llm_messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    llm_messages.append({"role": "user", "content": prompt})

    return await complete(
        llm_messages,
        settings,
        temperature=0.3,
    )
