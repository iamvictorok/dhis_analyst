"""Metadata resolver node.

Resolution path:
1. pgvector cosine similarity — when Postgres + pgvector is configured AND embeddings are enabled
2. Keyword match against the synced metadata index — works on both SQLite and Postgres
3. LLM re-ranking — when candidates are found but confidence is low,
   uses the LLM to pick the best match from candidates

Confidence gate: if the top match is below metadata_confidence_threshold, the
resolver auto-resolves to the best available match (logging a warning) rather
than blocking the pipeline.  Infrastructure failures (index unavailable/unseeded)
are distinguished from genuine misses so the user is never told a valid indicator
"doesn't exist".
"""
from __future__ import annotations

import json
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

    # 2. Keyword resolution — works on BOTH backends.
    # Pass search_alternatives from the LLM intent for broader matching.
    search_alts = state.get("metric_search_terms") or []
    if low_confidence:
        try:
            metrics = await _resolve_indicators_by_keyword(
                metrics, settings, search_alternatives=search_alts,
            )
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]
        except Exception as exc:
            logger.warning("keyword_indicator_resolve_failed", extra={"error": str(exc)})
            metadata_lookup_failed = True

    # 3. LLM re-ranking — when candidates exist but confidence is low, ask the
    #    LLM to pick the best match. This bridges the gap between layperson
    #    language and DHIS2 indicator names.
    if low_confidence and not metadata_lookup_failed and settings.llm_provider != "mock":
        try:
            metrics = await _llm_rerank_resolve(
                metrics, state, settings, threshold,
            )
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]
        except Exception as exc:
            logger.warning("llm_rerank_failed", extra={"error": str(exc)})

    # 4. Auto-resolve: if still low confidence, take the best available match
    #    instead of blocking the pipeline. The user asked in natural language —
    #    they shouldn't need to know DHIS2 indicator names.
    if low_confidence:
        labels = [
            m["label"] for m in low_confidence
            if isinstance(m.get("label"), str) and m["label"].strip()
        ]
        if metadata_lookup_failed:
            logger.warning(
                "metadata_index_unavailable_proceeding",
                extra={"low_confidence_metrics": labels},
            )
        else:
            # Auto-resolve: bump confidence of best match so it clears the gate
            for m in metrics:
                if m.get("uid") and m["uid"] != "UNRESOLVED" and m["uid_confidence"] < threshold:
                    logger.info(
                        "auto_resolve_low_confidence",
                        extra={
                            "label": m["label"],
                            "uid": m["uid"],
                            "original_confidence": m["uid_confidence"],
                        },
                    )
                    m["uid_confidence"] = threshold  # promote to pass the gate
            state["metrics"] = metrics

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
                # Do not block with a clarification. Let the analytics builder fall back to USER_ORGUNIT.
                logger.warning(
                    "org_unit_not_found_proceeding",
                    extra={"label": org_label, "using_placeholder": org_uid},
                )
        except Exception as exc:
            # Infra failure — leave the org unit as-is and let the analytics
            # builder fall back to USER_ORGUNIT; do not block with a clarification.
            logger.warning("org_unit_resolve_failed", extra={"label": org_label, "error": str(exc)})

    # --- Disaggregation resolution ---
    resolved_disaggregations = []
    for d in (state.get("disaggregations") or []):
        if d == "ou_children":
            resolved_disaggregations.append(d)
        else:
            # It's a demographic category string
            from backend.app.db.metadata_index import search_category_by_name
            from backend.app.db.session import get_db_session
            
            cat_resolved = None
            if settings.is_postgres and settings.embedding_provider != "mock":
                cat_resolved = await _pgvector_resolve_category(d, settings)
                
            if not cat_resolved:
                try:
                    async with get_db_session() as session:
                        results = await search_category_by_name(session, d, is_postgres=settings.is_postgres, limit=1)
                        if results and results[0].get("uid_confidence", 0) >= settings.metadata_confidence_threshold:
                            cat_resolved = {"uid": results[0]["uid"], "label": results[0]["label"]}
                except Exception as exc:
                    logger.warning("category_resolve_failed", extra={"term": d, "error": str(exc)})

            if cat_resolved:
                resolved_disaggregations.append(cat_resolved["uid"])
                logger.info("category_resolved", extra={"term": d, "uid": cat_resolved["uid"], "label": cat_resolved["label"]})
            else:
                logger.warning("category_not_found", extra={"term": d})
                
    state["disaggregations"] = resolved_disaggregations

    duration = time.time() - start_time
    logger.info(
        "resolve_metadata_success",
        extra={
            "duration_seconds": round(duration, 4),
            "clarification_needed": state.get("clarification_needed", False),
            "org_unit_uid": state["org_unit"].get("uid"),
            "resolved_metrics": [
                {"label": m["label"], "uid": m["uid"], "conf": m.get("uid_confidence")}
                for m in state["metrics"]
            ],
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
    search_alternatives: list[str] | None = None,
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

            # Merge global search_alternatives with per-metric search_terms
            # from the LLM intent — per-metric terms are more targeted.
            combined_alts = list(search_alternatives or [])
            per_metric_terms = metric.get("search_terms")
            if isinstance(per_metric_terms, list):
                for t in per_metric_terms:
                    t_s = str(t).strip()
                    if t_s and t_s.lower() not in {a.lower() for a in combined_alts}:
                        combined_alts.append(t_s)

            results = await search_indicator_by_name(
                session,
                metric["label"],
                is_postgres=settings.is_postgres,
                limit=5,
                search_alternatives=combined_alts or None,
            )

            if results:
                best = results[0]
                logger.info(
                    "indicator_resolved",
                    extra={
                        "original_label": metric["label"],
                        "original_uid": metric.get("uid"),
                        "resolved_uid": best["uid"],
                        "resolved_label": best["label"],
                        "confidence": best.get("uid_confidence"),
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
                    "indicator_not_found",
                    extra={"label": metric["label"], "uid": metric.get("uid")},
                )
                # Keep original but mark low confidence
                metric["uid_confidence"] = 0.50
                resolved.append(metric)

    return resolved


async def _llm_rerank_resolve(
    metrics: list[dict],
    state: AgentState,
    settings: Settings,
    threshold: float,
) -> list[dict]:
    """Use the LLM to pick the best indicator from broad DB candidates.

    When keyword search returns results but confidence is below threshold, this
    sends the user's original query + the top candidates to the LLM and asks it
    to pick the best match. This bridges layperson language to DHIS2 terms with
    near-perfect accuracy since the LLM understands medical synonyms, context,
    and intent.
    """
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session
    from backend.app.llm import complete

    user_query = ""
    if state.get("messages"):
        user_query = state["messages"][-1].get("content", "")

    resolved = []
    for metric in metrics:
        if metric.get("uid_confidence", 0) >= threshold:
            resolved.append(metric)
            continue

        # Grab broader candidates for the LLM to choose from
        async with get_db_session() as session:
            candidates = await search_indicator_by_name(
                session,
                metric["label"],
                is_postgres=settings.is_postgres,
                limit=10,
            )

        if not candidates:
            resolved.append(metric)
            continue

        # Build the LLM re-ranking prompt
        candidate_list = "\n".join(
            f"  {i+1}. \"{c['label']}\" (uid: {c['uid']}, type: {c.get('object_type', 'indicator')})"
            for i, c in enumerate(candidates)
        )

        rerank_prompt = f"""The user asked: "{user_query}"
The system extracted the health indicator: "{metric['label']}"

Below are the closest matches found in the DHIS2 metadata database.
Pick the ONE best match that most closely corresponds to what the user is asking about.
Return ONLY a JSON object: {{"index": <1-based index>, "reasoning": "<brief reason>"}}

Candidates:
{candidate_list}

If NONE of the candidates are relevant, return: {{"index": 0, "reasoning": "no match"}}"""

        try:
            raw = await complete(
                [{"role": "user", "content": rerank_prompt}],
                settings,
                json_mode=True,
                temperature=0.0,
            )
            parsed = json.loads(raw)
            idx = int(parsed.get("index", 0))
            if 1 <= idx <= len(candidates):
                chosen = candidates[idx - 1]
                logger.info(
                    "llm_rerank_resolved",
                    extra={
                        "original_label": metric["label"],
                        "chosen_label": chosen["label"],
                        "chosen_uid": chosen["uid"],
                        "reasoning": parsed.get("reasoning", ""),
                    },
                )
                resolved.append({
                    "label": chosen["label"],
                    "uid": chosen["uid"],
                    "uid_confidence": 0.90,  # LLM-verified match
                    "object_type": chosen.get("object_type", "indicator"),
                })
            else:
                logger.info("llm_rerank_no_match", extra={"label": metric["label"]})
                resolved.append(metric)
        except Exception as exc:
            logger.warning(
                "llm_rerank_parse_failed",
                extra={"label": metric["label"], "error": str(exc)},
            )
            # Fall back to the best keyword match
            if candidates:
                best = candidates[0]
                best["uid_confidence"] = max(best.get("uid_confidence", 0.70), 0.72)
                resolved.append(best)
            else:
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
    """Return up to 5 disambiguation candidates from the synced metadata DB."""
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session

    try:
        async with get_db_session() as session:
            results = await search_indicator_by_name(
                session,
                query,
                is_postgres=settings.is_postgres,
                limit=5,
            )
        return results
    except Exception as exc:
        logger.warning("keyword_candidates_db_failed", extra={"error": str(exc)})
        return []

async def _pgvector_resolve_category(
    term: str,
    settings: Settings,
) -> dict | None:
    """Query pgvector for a category match."""
    from backend.app.llm import embed
    from backend.app.db.session import get_db_session
    from sqlalchemy import text as sa_text

    try:
        async with get_db_session() as session:
            embedding = await embed(term, settings)
            if not embedding:
                return None

            result = await session.execute(
                sa_text("""
                SELECT uid, name, object_type, 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
                FROM metadata_index
                WHERE embedding IS NOT NULL
                  AND object_type = 'category'
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT 1
                """),
                {"emb": str(embedding)},
            )
            row = result.fetchone()
            if row and float(row[3]) >= settings.metadata_confidence_threshold:
                return {
                    "uid": row[0],
                    "label": row[1],
                }
    except Exception as exc:
        logger.warning("pgvector_resolve_category_failed", extra={"term": term, "error": str(exc)})
    return None
