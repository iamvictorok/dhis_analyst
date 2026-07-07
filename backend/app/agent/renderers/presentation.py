"""Presentation renderer — complete slide manifest for PPTX generation.

Generates a structured slide manifest: title, executive summary, data chart,
data table, recommendations, and sources slides.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.renderer.presentation")


def render_presentation(state: AgentState) -> list[dict]:
    """Build a complete slide manifest for PPTX generation."""
    metrics = state.get("metrics", [])
    metric = metrics[0]["label"] if metrics else "Health Indicator"
    org = state["org_unit"]["label"]
    periods = state.get("periods", [])
    period_text = ", ".join(periods)
    rows = state.get("dhis2_data", {}).get("rows", [])
    now = datetime.now(timezone.utc).strftime("%d %B %Y")

    logger.info(
        "render_presentation_start",
        extra={
            "metric": metric,
            "org_unit": org,
            "periods": periods,
            "rows_count": len(rows),
        }
    )

    # Parse rows once into aligned (org, period, value) triples, skipping any
    # row whose value cell isn't numeric (empty no-data cells, or a mis-shaped
    # row). This keeps x/y/labels the same length and avoids an unguarded
    # float() crashing the whole request after data was fetched.
    parsed_rows: list[tuple[str, str, float]] = []
    for row in rows:
        if len(row) <= 2:
            continue
        try:
            value = float(row[2])
        except (ValueError, TypeError):
            continue
        org_cell = str(row[0]) if len(row) > 0 else ""
        period_cell = str(row[1]) if len(row) > 1 else ""
        parsed_rows.append((org_cell, period_cell, value))

    values = [v for _, _, v in parsed_rows]

    slides: list[dict] = []

    # Slide 1: Title
    slides.append({
        "type": "title",
        "title": f"{metric} Briefing",
        "content": f"{org} — {period_text}\nGenerated {now}",
    })

    # Slide 2: Executive Summary
    summary = f"{len(rows)} data points from DHIS2 analytics."
    if len(values) >= 2 and values[0] > 0:
        pct = ((values[-1] - values[0]) / values[0]) * 100
        direction = "increased" if pct > 0 else "decreased"
        summary += f"\n{metric} {direction} by {abs(pct):.1f}%."
    if values:
        summary += f"\nTotal: {sum(values):,.0f} | Average: {sum(values) / len(values):,.1f}"

    slides.append({
        "type": "content",
        "title": "Executive Summary",
        "content": summary,
    })

    # Slide 3: Chart — x/y/labels drawn from the same parsed rows so they stay
    # aligned and never contain an unparseable value.
    chart_configs = state.get("active_chart_configs", [])
    slides.append({
        "type": "chart",
        "title": f"{metric} Trend",
        "content": chart_configs[0] if chart_configs else {},
        "data": {
            "x": [period for _, period, _ in parsed_rows],
            "y": [value for _, _, value in parsed_rows],
            "labels": [org for org, _, _ in parsed_rows],
        },
    })

    # Slide 4: Data Table
    table_content = f"{'Period':<15} {'Value':>12}\n{'─' * 28}\n"
    for row in rows[:15]:
        period = str(row[1]) if len(row) > 1 else "—"
        value = str(row[2]) if len(row) > 2 else "—"
        table_content += f"{period:<15} {value:>12}\n"
    if len(rows) > 15:
        table_content += f"\n… {len(rows) - 15} more rows"

    slides.append({
        "type": "table",
        "title": "Data Overview",
        "content": table_content,
    })

    # Slide 5: Recommendations
    recommendations = [
        "Validate metadata matches against the target DHIS2 instance.",
        "Investigate outliers and low-performing organisation units.",
        "Review data completeness and timeliness in DHIS2 data quality reports.",
        "Document follow-up decisions and assign accountability.",
    ]
    slides.append({
        "type": "content",
        "title": "Recommended Actions",
        "content": "\n".join(f"• {r}" for r in recommendations),
    })

    # Slide 6: Sources
    sources = [f"DHIS2 Analytics API — {len(metrics)} indicator(s)"]
    for ctx in state.get("web_context", []):
        if not ctx.get("url", "").startswith("local://"):
            sources.append(f"{ctx.get('title', 'External')}: {ctx.get('url', '')}")
    slides.append({
        "type": "content",
        "title": "Data Sources",
        "content": "\n".join(f"• {s}" for s in sources[:8]),
    })

    logger.info("render_presentation_complete", extra={"slides_count": len(slides)})
    return slides
