"""Report renderer — structured HTML for Tiptap editor and DOCX/PDF export.

Generates a full structured report: title, executive summary, key findings,
data table, trend analysis, recommendations, and data sources.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import logging

from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.renderer.report")


def render_report(state: AgentState) -> str:
    """Build a full structured report HTML document."""
    rows = state.get("dhis2_data", {}).get("rows", [])
    headers = state.get("dhis2_data", {}).get("headers", [])
    metrics = state.get("metrics", [])
    metric = escape(metrics[0]["label"]) if metrics else "Health Indicator"
    org = escape(state["org_unit"]["label"])
    periods = state.get("periods", [])
    period_text = escape(", ".join(periods))
    source = state.get("dhis2_data", {}).get("metadata", {}).get("data_source", "analytics_api")
    now = datetime.now(timezone.utc).strftime("%d %B %Y")

    logger.info(
        "render_report_start",
        extra={
            "metric": metric,
            "org_unit": org,
            "periods": periods,
            "rows_count": len(rows),
        }
    )

    # Parse (period, value) pairs together in one pass so a value always keeps
    # its own period label. Collecting values separately and later indexing
    # rows[i] misaligns every pair after a skipped (non-numeric) row.
    parsed: list[tuple[str, float]] = []
    for row in rows:
        if len(row) <= 2:
            continue
        try:
            value = float(row[2])
        except (ValueError, TypeError):
            continue
        period = str(row[1]) if len(row) > 1 else ""
        parsed.append((period, value))

    values = [v for _, v in parsed]
    total = sum(values)
    avg = total / len(values) if values else 0

    # --- Build HTML ---
    parts: list[str] = []

    # Title
    parts.append(f'<h1>{metric} Review — {org}</h1>')
    parts.append(f'<p><em>Generated {now} | Periods: {period_text} | Data source: {escape(source)}</em></p>')

    # Executive Summary
    parts.append('<h2>Executive Summary</h2>')
    trend_text = ""
    if len(values) >= 2 and values[0] > 0:
        pct = ((values[-1] - values[0]) / values[0]) * 100
        direction = "increased" if pct > 0 else "decreased"
        trend_text = f" The indicator {direction} by {abs(pct):.1f}% over the analysis period."
    parts.append(
        f'<p>This report analyses {metric} for {org} across {len(periods)} period(s) '
        f'({period_text}). A total of {len(rows)} data points were retrieved from the '
        f'DHIS2 {escape(source)}.{trend_text}</p>'
    )

    # Key Findings
    parts.append('<h2>Key Findings</h2>')
    parts.append('<ul>')
    parts.append(f'<li>Total aggregate value: <strong>{total:,.0f}</strong></li>')
    if values:
        parts.append(f'<li>Average: <strong>{avg:,.1f}</strong></li>')
        parts.append(f'<li>Range: {min(values):,.0f} (min) to {max(values):,.0f} (max)</li>')
    if trend_text:
        parts.append(f'<li>{trend_text.strip()}</li>')
    parts.append('</ul>')

    # Data Table
    parts.append('<h2>Data Table</h2>')
    table_headers = headers or ["Organisation Unit", "Period", "Value", "Metric"]
    parts.append('<table>')
    parts.append('<thead><tr>')
    for h in table_headers:
        parts.append(f'<th>{escape(str(h))}</th>')
    parts.append('</tr></thead>')
    parts.append('<tbody>')
    for row in rows:
        parts.append('<tr>')
        for cell in row:
            parts.append(f'<td>{escape(str(cell))}</td>')
        parts.append('</tr>')
    parts.append('</tbody></table>')

    # Trend Analysis
    if len(parsed) >= 2:
        parts.append('<h2>Trend Analysis</h2>')
        parts.append('<p>Period-over-period changes:</p>')
        parts.append('<ul>')
        for i in range(1, len(parsed)):
            prev_period, prev_val = parsed[i - 1]
            curr_period, curr_val = parsed[i]
            prev_period = prev_period or f"Period {i}"
            curr_period = curr_period or f"Period {i + 1}"
            if prev_val > 0:
                change = ((curr_val - prev_val) / prev_val) * 100
                direction = "↑" if change > 0 else "↓"
                parts.append(
                    f'<li>{escape(prev_period)} → {escape(curr_period)}: '
                    f'{direction} {abs(change):.1f}% ({prev_val:,.0f} → {curr_val:,.0f})</li>'
                )
        parts.append('</ul>')

    # Recommendations
    parts.append('<h2>Recommendations</h2>')
    parts.append('<ol>')
    parts.append('<li>Validate that the metadata UID matches accurately before using these '
                 'figures for operational decisions.</li>')
    if values and min(values) < avg * 0.5:
        parts.append('<li>Investigate low-performing periods/org units that fall below 50% '
                     'of the average value.</li>')
    parts.append('<li>Review data quality flags for completeness and timeliness in DHIS2 '
                 'data quality reports.</li>')
    parts.append('<li>Discuss findings with programme teams and agree on follow-up actions.</li>')
    parts.append('</ol>')

    # Data Sources
    parts.append('<h2>Data Sources</h2>')
    parts.append('<ul>')
    parts.append(f'<li>DHIS2 Analytics ({escape(source)}) — '
                 f'{len(metrics)} indicator(s) queried</li>')
    web_items = [c for c in state.get("web_context", []) if not c.get("url", "").startswith("local://")]
    for item in web_items[:5]:
        parts.append(f'<li><a href="{escape(item.get("url", ""))}">'
                     f'{escape(item.get("title", "External source"))}</a></li>')
    parts.append('</ul>')

    result = "\n".join(parts)
    logger.info("render_report_complete", extra={"html_length": len(result)})
    return result
