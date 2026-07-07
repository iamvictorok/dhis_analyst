"""Metadata resolver node.

Resolution path:
1. pgvector cosine similarity — when Postgres + pgvector is configured AND embeddings are enabled
2. Keyword match against the synced metadata index — works on both SQLite and Postgres

Confidence gate: if the top match is below metadata_confidence_threshold, surfaces
disambiguation candidates to the user rather than silently guessing. Infrastructure
failures (index unavailable/unseeded) are distinguished from genuine misses so the
user is never told a valid indicator "doesn't exist".
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
    # Track whether a resolution attempt failed because the metadata index was
    # unavailable (unseeded table, locked DB, provider down) as opposed to the
    # index being reachable but holding no match. The two demand opposite
    # responses: an infra failure must NOT tell the user their indicator is
    # invalid, while a genuine miss should surface disambiguation candidates.
    threshold = settings.metadata_confidence_threshold
    metadata_lookup_failed = False
    low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]

    # 1. pgvector semantic resolution (Postgres with real embeddings only).
    if low_confidence and settings.is_postgres:
        try:
            metrics = await _pgvector_resolve(metrics, settings)
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]
        except Exception as exc:
            logger.warning("pgvector_resolve_failed", extra={"error": str(exc)})
            metadata_lookup_failed = True

    # 2. Keyword resolution — works on BOTH backends (the query targets the
    # right table per settings.is_postgres). Previously gated to SQLite, which
    # dead-ended every Postgres query whenever embeddings were mock/unavailable.
    if low_confidence:
        try:
            metrics = await _resolve_indicators_by_keyword(metrics, settings)
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]
        except Exception as exc:
            logger.warning("keyword_indicator_resolve_failed", extra={"error": str(exc)})
            metadata_lookup_failed = True

    if low_confidence:
        labels = [
            m["label"] for m in low_confidence
            if isinstance(m.get("label"), str) and m["label"].strip()
        ]
        if metadata_lookup_failed:
            # Infrastructure problem, not a bad query. Proceed with the
            # unresolved metric; downstream fetch (mock or real) surfaces any
            # real issue instead of a misleading "indicator doesn't exist" loop.
            logger.warning(
                "metadata_index_unavailable_proceeding",
                extra={"low_confidence_metrics": labels},
            )
        else:
            # Genuine low/no-confidence match — query candidates PER label so a
            # multi-indicator request doesn't collapse into one junk LIKE.
            candidates: list[dict] = []
            for label in labels:
                candidates.extend(await _keyword_candidates_from_db(label, settings))
            if candidates:
                seen: set[str] = set()
                unique_names = []
                for c in candidates:
                    if c["label"] not in seen:
                        seen.add(c["label"])
                        unique_names.append(c["label"])
                names = ", ".join(f'"{n}"' for n in unique_names[:3])
                state["clarification_needed"] = True
                state["clarification_question"] = (
                    f"I found a low-confidence match for your indicator. Did you mean one of: {names}? "
                    f"Please rephrase your question with the exact indicator name."
                )
                logger.info(
                    "disambiguation_triggered",
                    extra={"low_confidence_metrics": labels, "candidates": names},
                )
            else:
                state["clarification_needed"] = True
                missing_labels = ", ".join(labels) or "the requested indicator"
                state["clarification_question"] = (
                    f"I couldn't find any health indicators matching '{missing_labels}' in this DHIS2 instance. "
                    f"Could you verify the indicator name and try again?"
                )
                logger.info(
                    "metadata_low_confidence_no_candidates",
                    extra={"low_confidence_metrics": labels},
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
                # Index reachable but no match for an explicitly named location.
                # Ask the user rather than sending ou:UNRESOLVED to DHIS2 (409)
                # or silently querying the wrong scope. Metric clarification (if
                # any) takes precedence — don't overwrite it.
                logger.warning(
                    "org_unit_not_found",
                    extra={"label": org_label, "using_placeholder": org_uid},
                )
                if not state.get("clarification_needed"):
                    state["clarification_needed"] = True
                    state["clarification_question"] = (
                        f"I couldn't find a location named '{org_label}' in this DHIS2 instance. "
                        f"Could you check the spelling, or try a broader area?"
                    )
        except Exception as exc:
            # Infra failure — leave the org unit as-is and let the analytics
            # builder fall back to USER_ORGUNIT; do not block with a clarification.
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


async def _uid_exists(session, uid: str, settings: Settings) -> bool:
    """Return True if ``uid`` is present in the synced metadata index."""
    from sqlalchemy import text as sa_text

    table = "metadata_index" if settings.is_postgres else "metadata_index_lite"
    try:
        result = await session.execute(
            sa_text(f"SELECT 1 FROM {table} WHERE uid = :uid LIMIT 1"),
            {"uid": uid},
        )
        return result.fetchone() is not None
    except Exception as exc:
        # If we can't check, don't trust the UID — fall through to resolution.
        logger.warning("uid_existence_check_failed", extra={"uid": uid, "error": str(exc)})
        return False


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


async def _resolve_indicators_by_keyword(
    metrics: list[dict],
    settings: Settings,
) -> list[dict]:
    """Resolve indicator UIDs via keyword search against the synced index.

    Works on both SQLite and Postgres (the query targets the right table).
    Replaces LLM-guessed UIDs with real ones from the index. A UID is only
    trusted (and DB lookup skipped) when it is confident AND actually present
    in the index — LLMs hallucinate syntactically valid UIDs, so shape alone is
    not enough.
    """
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session

    resolved = []
    async with get_db_session() as session:
        for metric in metrics:
            uid = metric.get("uid", "")
            # Skip resolution only for a high-confidence UID that we can confirm
            # exists in the index (a real prior DB/pgvector resolution).
            if (
                _looks_like_dhis2_uid(uid)
                and metric.get("uid_confidence", 0) >= settings.metadata_confidence_threshold
                and await _uid_exists(session, uid, settings)
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
                WHERE embedding IS NOT NULL
                  AND object_type IN ('indicator', 'dataElement', 'programIndicator')
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
