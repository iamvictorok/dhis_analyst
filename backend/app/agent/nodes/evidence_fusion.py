"""Evidence fusion layer — active when EVIDENCE_FUSION=true.

Tags every insight with source attribution, confidence, and provenance.
Aggregates evidence from:
- DHIS2 analytics data (metric UIDs, data source)
- Tavily web context (URLs, relevance scores)
- LLM-generated claims (when LLM provider is live)
"""
from __future__ import annotations

import logging

from backend.config import Settings
from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.evidence_fusion")


async def fuse_evidence(state: AgentState, settings: Settings) -> AgentState:
    logger.info(
        "evidence_fusion_start",
        extra={
            "session_id": state.get("session_id"),
            "metrics_count": len(state.get("metrics", [])),
            "web_context_count": len(state.get("web_context", [])),
        }
    )
    if not settings.evidence_fusion:
        state["evidence_items"] = []
        logger.info("evidence_fusion_skipped", extra={"reason": "disabled_by_settings"})
        return state

    items: list[dict] = []

    # --- DHIS2 data source evidence ---
    data = state.get("dhis2_data", {})
    data_source = data.get("metadata", {}).get("data_source", "unknown")
    row_count = len(data.get("rows", []))

    for metric in state.get("metrics", []):
        items.append({
            "claim": (
                f"Analysis uses \"{metric['label']}\" ({metric['uid']}) from DHIS2 "
                f"{data_source}. {row_count} data points retrieved."
            ),
            "source": "dhis2",
            "source_detail": f"{metric['label']} ({metric['uid']}) via {data_source}",
            "confidence": metric.get("uid_confidence", 0.0),
        })

    # --- Trend evidence (computed from data) ---
    # Only meaningful for one metric in one org unit over several periods.
    # DHIS2 rows arrive in no guaranteed order, so sort by the chronological
    # period list in metaData before comparing first vs last, and name the
    # actual periods (not the relative code, e.g. "LAST_12_MONTHS").
    rows = data.get("rows", [])
    if len(rows) >= 2 and state.get("metrics"):
        try:
            orgs = {str(r[0]) for r in rows}
            metric_names = {str(r[3]) for r in rows if len(r) > 3}
            if len(orgs) == 1 and len(metric_names) <= 1:
                meta = data.get("metadata", {}).get("raw_metadata", {}) or {}
                items_meta = meta.get("items") or {}
                order = [
                    str((items_meta.get(pid) or {}).get("name") or pid)
                    for pid in ((meta.get("dimensions") or {}).get("pe") or [])
                ]
                series = sorted(
                    ((str(r[1]), float(r[2])) for r in rows if len(r) > 2),
                    key=lambda pv: order.index(pv[0]) if pv[0] in order else len(order),
                )
                if len(series) >= 2 and series[0][1] > 0:
                    (p0, v0), (p1, v1) = series[0], series[-1]
                    change = ((v1 - v0) / v0) * 100
                    direction = "increased" if change > 0 else "decreased"
                    items.append({
                        "claim": (
                            f"{state['metrics'][0]['label']} {direction} by "
                            f"{abs(change):.1f}% from {p0} ({v0:g}) to {p1} ({v1:g}) "
                            f"in {next(iter(orgs))}."
                        ),
                        "source": "dhis2",
                        "source_detail": "Computed from retrieved data rows",
                        "confidence": 0.95 if len(series) > 2 else 0.80,
                    })
        except (ValueError, TypeError, IndexError):
            pass

    # --- Web enrichment evidence ---
    for ctx in state.get("web_context", []):
        url = ctx.get("url", "")
        if url.startswith("local://"):
            continue
        items.append({
            "claim": f"External context: {ctx.get('title', 'Unknown source')}",
            "source": "tavily",
            "source_detail": url,
            "confidence": float(ctx.get("score", 0.7)),
        })

    state["evidence_items"] = items
    logger.info(
        "evidence_fused",
        extra={
            "session_id": state["session_id"],
            "item_count": len(items),
            "sources": list({i["source"] for i in items}),
        },
    )
    return state
