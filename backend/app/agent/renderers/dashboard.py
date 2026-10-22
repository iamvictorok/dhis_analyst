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

    period_order = _period_order(state)

    for metric_name, m_rows in metric_data.items():
        orgs_in_rows = list(dict.fromkeys(str(r[0]) for r in m_rows))
        periods_in_rows = list(dict.fromkeys(str(r[1]) for r in m_rows))
        periods_sorted = sorted(
            periods_in_rows,
            key=lambda p: (period_order.index(p) if p in period_order else len(period_order), p),
        )
        multi_org = len(orgs_in_rows) > 1
        single_period = len(periods_sorted) == 1

        # Build plotting series. DHIS2 returns rows in no guaranteed order and,
        # for "by district" questions, one row per org unit per period. Plotting
        # them as one flat series mislabels every bar (the old "National"
        # bug), so: one org -> periods on x; many orgs + one period -> org
        # units on x (ranked); many orgs + many periods -> one series per org.
        if multi_org and single_period:
            by_org = {str(r[0]): _safe_float(r[2]) for r in m_rows}
            ranked = sorted(by_org.items(), key=lambda kv: kv[1], reverse=True)
            x_axis_title = "Organisation unit"
            plot_series = [{
                "name": f"{metric_name} ({periods_sorted[0]})",
                "x": [k for k, _ in ranked],
                "y": [v for _, v in ranked],
            }]
            title = f"{metric_name} by organisation unit — {periods_sorted[0]}"
        elif multi_org:
            x_axis_title = "Period"
            plot_series = []
            for o in orgs_in_rows:
                vals = {str(r[1]): _safe_float(r[2]) for r in m_rows if str(r[0]) == o}
                plot_series.append({
                    "name": o,
                    "x": [p for p in periods_sorted if p in vals],
                    "y": [vals[p] for p in periods_sorted if p in vals],
                })
            title = f"{metric_name} by organisation unit"
        else:
            vals = {str(r[1]): _safe_float(r[2]) for r in m_rows}
            x_axis_title = "Period"
            plot_series = [{
                "name": metric_name,
                "x": periods_sorted,
                "y": [vals[p] for p in periods_sorted],
            }]
            title = f"{metric_name} — {orgs_in_rows[0] if orgs_in_rows else org}"

        x_values = plot_series[0]["x"] if len(plot_series) == 1 else periods_sorted
        y_values = plot_series[0]["y"] if len(plot_series) == 1 else []
        table_rows = sorted(
            m_rows,
            key=lambda r: (str(r[0]), period_order.index(str(r[1])) if str(r[1]) in period_order else 0),
        )

        # Deterministic chart ID based on content
        chart_id = hashlib.sha256(
            f"{metric_name}:{':'.join(orgs_in_rows)}:{':'.join(periods_sorted)}".encode()
        ).hexdigest()[:12]

        for viz_type in viz_types:
            if viz_type in ("bar", "line"):
                charts.append({
                    "id": f"{viz_type}-{chart_id}",
                    "title": title,
                    "type": viz_type,
                    "series": plot_series,
                    "axes": {"x": x_axis_title, "y": "Value"},
                    "layout": _dark_layout(title, x_axis_title, "Value"),
                })
            elif viz_type == "pie" and len(plot_series) == 1:
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
                    "series": [dict(s, mode="markers") for s in plot_series],
                    "axes": {"x": x_axis_title, "y": "Value"},
                    "layout": _dark_layout(f"{metric_name} Scatter", x_axis_title, "Value"),
                })
            elif viz_type == "table" or (viz_type == "pie" and len(plot_series) != 1):
                charts.append({
                    "id": f"table-{chart_id}",
                    "title": f"{metric_name} Data Table",
                    "type": "table",
                    "series": [{
                        "header": ["Organisation Unit", "Period", "Value"],
                        "cells": [
                            [str(r[0]) for r in table_rows],
                            [str(r[1]) for r in table_rows],
                            [str(r[2]) for r in table_rows],
                        ],
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


def _period_order(state: AgentState) -> list[str]:
    """Chronological period display names, from DHIS2's metaData.dimensions.pe."""
    meta = state.get("dhis2_data", {}).get("metadata", {}).get("raw_metadata", {}) or {}
    pe_ids = (meta.get("dimensions") or {}).get("pe") or []
    items = meta.get("items") or {}
    order = []
    for pid in pe_ids:
        name = (items.get(pid) or {}).get("name")
        order.append(str(name) if name else str(pid))
    return order


def _safe_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
