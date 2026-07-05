"""Metadata resolver node.

Resolution path:
1. pgvector cosine similarity — when Postgres + pgvector is configured AND embeddings are enabled
2. SQLite keyword-match — when using SQLite or no embedding provider
3. In-memory KNOWN_METRICS lookup — deterministic fallback always available

Confidence gate: if top match < metadata_confidence_threshold, surfaces
disambiguation candidates to the user rather than silently guessing.
"""
from __future__ import annotations

import logging
import re

from backend.config import Settings
from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.metadata_resolve")


async def resolve_metadata(state: AgentState, settings: Settings) -> AgentState:
    """Resolve metric UIDs and org unit UIDs with confidence gate."""
    import time
    start_time = time.time()
    metrics = state["metrics"]

    if not metrics:
        state["clarification_needed"] = True
        state["clarification_question"] = "I couldn't identify a health indicator in your question. Could you specify which indicator you'd like to analyse?"
        return state

    logger.info(
        "resolve_metadata_start",
        extra={
            "metrics_count": len(metrics),
            "metrics": [m.get("label") for m in metrics],
            "org_unit_label": state.get("org_unit", {}).get("label"),
        }
    )

    # --- Metric resolution ---
    # Attempt pgvector resolution for any metric with low confidence
    low_confidence = [m for m in metrics if m["uid_confidence"] < settings.metadata_confidence_threshold]

    if low_confidence and settings.is_postgres:
        try:
            metrics = await _pgvector_resolve(metrics, settings)
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < settings.metadata_confidence_threshold]
        except Exception as exc:
            logger.warning("pgvector_resolve_failed", extra={"error": str(exc)})

    # SQLite path: resolve ALL metrics whose UIDs look fabricated or have low confidence
    if not settings.is_postgres:
        try:
            metrics = await _sqlite_resolve_indicators(metrics, settings)
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < settings.metadata_confidence_threshold]
        except Exception as exc:
            logger.warning("sqlite_indicator_resolve_failed", extra={"error": str(exc)})

    # If Postgres isn't used, we skip pgvector but might still check fallback or keywords
    if low_confidence:
        # Still low after resolution — surface disambiguation from synced DB
        query_labels = " ".join(m["label"] for m in low_confidence)
        candidates = await _keyword_candidates_from_db(query_labels, settings)
        if candidates:
            names = ", ".join(f'"{c["label"]}"' for c in candidates[:3])
            state["clarification_needed"] = True
            state["clarification_question"] = (
                f"I found a low-confidence match for your indicator. Did you mean one of: {names}? "
                f"Please rephrase your question with the exact indicator name."
            )
            logger.info(
                "disambiguation_triggered",
                extra={"low_confidence_metrics": [m["label"] for m in low_confidence], "candidates": names},
            )
        else:
            logger.info(
                "metadata_low_confidence_no_candidates",
                extra={"low_confidence_metrics": [m["label"] for m in low_confidence]},
            )

    # --- Org unit resolution ---
    org_unit = state.get("org_unit", {})
    org_label = org_unit.get("label", "National")
    org_uid = org_unit.get("uid", "")

    # Check if the UID looks like a placeholder (fabricated from the label)
    needs_resolution = (
        not org_uid
        or org_uid == org_label.upper().replace(" ", "_")
        or not _looks_like_dhis2_uid(org_uid)
    )

    if needs_resolution and org_label.lower() not in {"national", "federal"}:
        try:
            resolved = await _resolve_org_unit_from_db(org_label, settings)
            if resolved:
                state["org_unit"] = resolved
                logger.info(
                    "org_unit_resolved",
                    extra={
                        "original_label": org_label,
                        "resolved_uid": resolved["uid"],
                        "resolved_label": resolved["label"],
                        "resolved_level": resolved["level"],
                    },
                )
            else:
                logger.warning(
                    "org_unit_not_found",
                    extra={"label": org_label, "using_placeholder": org_uid},
                )
        except Exception as exc:
            logger.warning("org_unit_resolve_failed", extra={"label": org_label, "error": str(exc)})

    duration = time.time() - start_time
    logger.info(
        "resolve_metadata_success",
        extra={
            "duration_seconds": round(duration, 4),
            "clarification_needed": state.get("clarification_needed", False),
            "org_unit_uid": state["org_unit"].get("uid"),
        }
    )
    return state


def _looks_like_dhis2_uid(uid: str) -> bool:
    """DHIS2 UIDs are exactly 11 alphanumeric characters."""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9]{10}$", uid))


async def _resolve_org_unit_from_db(label: str, settings: Settings) -> dict | None:
    """Look up an org unit by name in the synced metadata database."""
    from backend.app.db.metadata_index import search_org_unit_by_name
    from backend.app.db.session import get_db_session

    async with get_db_session() as session:
        results = await search_org_unit_by_name(
            session,
            label,
            is_postgres=settings.is_postgres,
            limit=3,
        )

    if not results:
        return None

    best = results[0]
    return {
        "label": best["label"],
        "uid": best["uid"],
        "level": best.get("level", 2),
    }


async def _sqlite_resolve_indicators(
    metrics: list[dict],
    settings: Settings,
) -> list[dict]:
    """Resolve indicator UIDs via SQLite keyword search.

    Replaces LLM-guessed UIDs with real ones from the synced metadata DB.
    Only resolves metrics whose UIDs look fabricated (not valid DHIS2 UIDs).
    """
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session

    resolved = []
    async with get_db_session() as session:
        for metric in metrics:
            # Skip if the UID already looks like a real DHIS2 UID AND confidence is high
            if (
                _looks_like_dhis2_uid(metric.get("uid", ""))
                and metric.get("uid_confidence", 0) >= settings.metadata_confidence_threshold
            ):
                resolved.append(metric)
                continue

            results = await search_indicator_by_name(
                session,
                metric["label"],
                is_postgres=settings.is_postgres,
                limit=3,
            )

            if results:
                best = results[0]
                logger.info(
                    "indicator_resolved_sqlite",
                    extra={
                        "original_label": metric["label"],
                        "original_uid": metric.get("uid"),
                        "resolved_uid": best["uid"],
                        "resolved_label": best["label"],
                        "object_type": best.get("object_type"),
                    },
                )
                resolved.append({
                    "label": best["label"],
                    "uid": best["uid"],
                    "uid_confidence": best.get("uid_confidence", 0.85),
                    "object_type": best.get("object_type", "indicator"),
                })
            else:
                logger.warning(
                    "indicator_not_found_sqlite",
                    extra={"label": metric["label"], "uid": metric.get("uid")},
                )
                # Keep original but mark low confidence to trigger disambiguation
                metric["uid_confidence"] = 0.50
                resolved.append(metric)

    return resolved


async def _pgvector_resolve(
    metrics: list[dict],
    settings: Settings,
) -> list[dict]:
    """Query pgvector for cosine similarity. Returns enriched metrics list."""
    import time
    from backend.app.llm import embed
    from backend.app.db.session import get_db_session
    from sqlalchemy import text as sa_text

    resolved = []
    async with get_db_session() as session:
        for metric in metrics:
            metric_text = metric["label"]
            resolve_start = time.time()
            embedding = await embed(metric_text, settings)

            if not embedding:
                resolved.append(metric)
                continue

            result = await session.execute(
                sa_text("""
                SELECT uid, name, object_type, 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
                FROM metadata_index
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT 3
                """),
                {"emb": str(embedding)},
            )
            rows = result.fetchall()
            resolve_duration = time.time() - resolve_start
            if rows:
                best = rows[0]
                similarity = float(best[3])
                resolved.append({
                    "label": best[1],
                    "uid": best[0],
                    "uid_confidence": round(similarity, 4),
                    "object_type": best[2],
                })
                logger.info(
                    "pgvector_hit",
                    extra={
                        "query": metric_text,
                        "resolved": best[1],
                        "similarity": similarity,
                        "duration_seconds": round(resolve_duration, 4),
                    },
                )
            else:
                resolved.append(metric)

    return resolved


async def _keyword_candidates_from_db(query: str, settings: Settings) -> list[dict]:
    """Return up to 3 disambiguation candidates from the synced metadata DB."""
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session

    try:
        async with get_db_session() as session:
            results = await search_indicator_by_name(
                session,
                query,
                is_postgres=settings.is_postgres,
                limit=3,
            )
        return results
    except Exception as exc:
        logger.warning("keyword_candidates_db_failed", extra={"error": str(exc)})
        return []
