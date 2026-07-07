"""Dashboard renderer — multi-metric Plotly chart configs.

Generates Plotly-compatible chart configuration objects that the frontend
renders via Plotly.js. Supports bar, line, pie, scatter, and table types.
"""
from __future__ import annotations

import hashlib
import logging
from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.renderer.dashboard")

_SUPPORTED_VIZ: frozenset[str] = frozenset({"bar", "line", "pie", "scatter", "table"})


def render_dashboard(state: AgentState) -> list[dict]:
    """Generate chart configs from DHIS2 data for Plotly rendering."""
    rows = state.get("dhis2_data", {}).get("rows", [])
    metrics = state.get("metrics", [])
    # Only render viz types we actually support; anything else (e.g. "map",
    # which the keyword fallback can emit) would silently produce no chart and
    # a blank dashboard. Default to a bar chart so there is always output.
    viz_types = [v for v in state.get("viz_types", ["bar"]) if v in _SUPPORTED_VIZ] or ["bar"]
    org = state["org_unit"]["label"]

    logger.info(
        "render_dashboard_start",
        extra={
            "viz_types": viz_types,
            "metrics_count": len(metrics),
            "rows_count": len(rows),
            "org_unit": org,
        }
    )

    if not rows or not metrics:
        logger.info("render_dashboard_skipped", extra={"reason": "missing rows or metrics"})
        return []

    charts: list[dict] = []

    # Group rows by metric
    metric_data: dict[str, list[list]] = {}
    for row in rows:
        metric_name = row[3] if len(row) > 3 else metrics[0]["label"]
        metric_data.setdefault(metric_name, []).append(row)

    for metric_name, m_rows in metric_data.items():
        x_values = [str(r[1]) for r in m_rows]  # Period
        y_values = [_safe_float(r[2]) for r in m_rows]  # Value
        orgs = [str(r[0]) for r in m_rows]  # Org unit

        # Deterministic chart ID based on content
        chart_id = hashlib.sha256(f"{metric_name}:{':'.join(x_values)}".encode()).hexdigest()[:12]

        for viz_type in viz_types:
            if viz_type in ("bar", "line"):
                charts.append({
                    "id": f"{viz_type}-{chart_id}",
                    "title": f"{metric_name} — {org}",
                    "type": viz_type,
                    "series": [{
                        "name": metric_name,
                        "x": x_values,
                        "y": y_values,
                    }],
                    "axes": {"x": "Period", "y": "Value"},
                    "layout": _dark_layout(f"{metric_name} — {org}", "Period", "Value"),
                })
            elif viz_type == "pie":
                charts.append({
                    "id": f"pie-{chart_id}",
                    "title": f"{metric_name} Distribution",
                    "type": "pie",
                    "series": [{
                        "labels": x_values,
                        "values": y_values,
                    }],
                    "layout": _dark_layout(f"{metric_name} Distribution"),
                })
            elif viz_type == "scatter":
                charts.append({
                    "id": f"scatter-{chart_id}",
                    "title": f"{metric_name} Scatter",
                    "type": "scatter",
                    "series": [{
                        "name": metric_name,
                        "x": x_values,
                        "y": y_values,
                        "mode": "markers",
                    }],
                    "axes": {"x": "Period", "y": "Value"},
                    "layout": _dark_layout(f"{metric_name} Scatter", "Period", "Value"),
                })
            elif viz_type == "table":
                charts.append({
                    "id": f"table-{chart_id}",
                    "title": f"{metric_name} Data Table",
                    "type": "table",
                    "series": [{
                        "header": ["Organisation Unit", "Period", "Value"],
                        "cells": [orgs, x_values, [str(v) for v in y_values]],
                    }],
                    "layout": _dark_layout(f"{metric_name} Data Table"),
                })

    logger.info("render_dashboard_complete", extra={"charts_count": len(charts)})
    return charts


def _dark_layout(title: str, xaxis: str = "", yaxis: str = "") -> dict:
    """Return a Plotly dark theme layout config."""
    layout: dict = {
        "title": {"text": title, "font": {"color": "#e5e7eb", "size": 16}},
        "paper_bgcolor": "#0b171d",
        "plot_bgcolor": "#0f2028",
        "font": {"color": "#9fb2bf", "family": "Inter, sans-serif"},
        "margin": {"l": 60, "r": 30, "t": 50, "b": 50},
        "colorway": [
            "#38bdf8", "#34d399", "#fbbf24", "#f87171",
            "#a78bfa", "#fb923c", "#22d3ee", "#e879f9",
        ],
    }
    if xaxis:
        layout["xaxis"] = {
            "title": xaxis,
            "gridcolor": "#1f3440",
            "zerolinecolor": "#334b5b",
        }
    if yaxis:
        layout["yaxis"] = {
            "title": yaxis,
            "gridcolor": "#1f3440",
            "zerolinecolor": "#334b5b",
        }
    return layout


def _safe_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
