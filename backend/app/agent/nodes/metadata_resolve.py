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

import asyncio
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

    reporting_rate_resolved = False
    # 0a. Data set reporting rates / completeness / timeliness. These are not
    #     indicators or data elements, so the cascade below can never find them
    #     and used to silently substitute an unrelated indicator.
    try:
        user_text_rr = ""
        for msg in reversed(state.get("messages") or []):
            if msg.get("role") == "user":
                user_text_rr = str(msg.get("content") or "")
                break
        rr = await _reporting_rate_metric(user_text_rr, settings, token=state.get("_dhis2_token"))
        if rr is not None:
            if rr.get("clarify"):
                state["clarification_needed"] = True
                state["clarification_question"] = rr["clarify"]
                return state
            state["metrics"] = [rr]
            metrics = state["metrics"]
            reporting_rate_resolved = True
            logger.info("reporting_rate_resolved", extra={"uid": rr["uid"], "label": rr["label"]})
    except Exception as exc:
        logger.warning("reporting_rate_scan_failed", extra={"error": str(exc)})

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
            resolved = await _resolve_org_unit_from_db_or_live_api(
                org_label, settings, token=state.get("_dhis2_token")
            )
            if resolved:
                resolved = {**resolved, "original_label": org_label}
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

    # --- Breakdown level ("by district", "each chiefdom", "facilities in Bo") ---
    try:
        await _apply_breakdown_level(state, settings)
    except Exception as exc:
        logger.warning("breakdown_level_failed", extra={"error": str(exc)})

    # 0. The user typed exact DHIS2 names (e.g. "IDSR Malaria"): trust them over
    #    whatever label the LLM paraphrased them into ("Malaria case").
    try:
        user_text = ""
        for msg in reversed(state.get("messages") or []):
            if msg.get("role") == "user":
                user_text = str(msg.get("content") or "")
                break
        exact = (
            await _exact_names_in_message(user_text, settings)
            if user_text and not reporting_rate_resolved else []
        )
        if exact and len(exact) == len(metrics):
            current = {str(m.get("label", "")).lower() for m in metrics}
            if current != {e["name"].lower() for e in exact}:
                logger.info(
                    "exact_name_override",
                    extra={"llm_labels": sorted(current), "exact_names": [e["name"] for e in exact]},
                )
            metrics = [
                {
                    **(metrics[i] if i < len(metrics) else {}),
                    "label": e["name"],
                    "uid": e["uid"],
                    "uid_confidence": 0.97,
                    "object_type": e["object_type"],
                }
                for i, e in enumerate(exact)
            ]
            state["metrics"] = metrics
    except Exception as exc:  # never block the pipeline on this shortcut
        logger.warning("exact_name_scan_failed", extra={"error": str(exc)})
    # --- Expert resolution: rank, probe for data, judge (match / ambiguous / not available) ---
    if not reporting_rate_resolved:
        try:
            outcome = await _expert_resolve(state, settings)
        except Exception as exc:
            logger.warning("expert_resolve_failed", extra={"error": str(exc)}, exc_info=True)
            outcome = {"status": "skip"}
        if outcome["status"] == "clarify":
            state["clarification_needed"] = True
            state["clarification_question"] = outcome["question"]
            return state
        if outcome["status"] == "not_available":
            state["not_available_message"] = outcome["message"]
            return state
        metrics = state["metrics"]

    low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]
    if reporting_rate_resolved:
        low_confidence = []

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

    # 3b. Live DHIS2 REST API Fallback — when local DB missed or left UID unresolved
    unresolved_metrics = [
        m for m in metrics
        if not m.get("uid") or m.get("uid") == "UNRESOLVED" or m.get("uid_confidence", 0) < threshold
    ]
    if unresolved_metrics:
        try:
            metrics = await _resolve_indicators_via_live_dhis2_api(
                metrics, settings, state, threshold,
            )
            state["metrics"] = metrics
            low_confidence = [m for m in metrics if m["uid_confidence"] < threshold]
        except Exception as exc:
            logger.warning("live_dhis2_api_indicator_resolve_failed", extra={"error": str(exc)})

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
            # Weak matches (a single shared word, or the LLM re-ranker said none
            # of the candidates fit) used to be promoted silently, e.g.
            # "COVID-19 booster doses" -> "PCV3 doses given". For a health
            # analyst a wrong indicator is worse than a question, so ask.
            weak = [
                m for m in metrics
                if m.get("uid") and m["uid"] != "UNRESOLVED"
                and m.get("uid_confidence", 0) < _WEAK_MATCH_CONFIDENCE
            ]
            if weak:
                question = await _weak_match_question(weak, settings)
                logger.info("weak_match_clarification", extra={"labels": [m.get("label") for m in weak]})
                state["clarification_needed"] = True
                state["clarification_question"] = question
                state["metrics"] = metrics
                return state
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

    # --- Numerator / denominator / formula questions on indicators ---
    if re.search(r"\b(numerators?|denominators?|formula|how\s+is\s+.+\s+calculated|calculation)\b",
                 _last_user_text(state), re.I):
        inds = [m for m in state.get("metrics") or [] if m.get("object_type") == "indicator"
                and _looks_like_dhis2_uid(m.get("uid") or "")]
        if inds:
            state["include_numden"] = True
            try:
                await _indicator_formulas(inds, state, settings)
            except Exception as exc:
                logger.warning("indicator_formula_lookup_failed", extra={"error": str(exc)})

    # --- Disaggregation resolution ---
    # Category breakdowns ("by age", "by sex") are only meaningful for data
    # elements whose category combo contains that category, so match the
    # user's word against the chosen data element's own categories first.
    raw_disaggs = list(state.get("disaggregations") or [])
    for m_ in re.finditer(r"\b(?:by|per|and|with)\s+(age(?:\s+group)?|sex|gender)\b|\b(age|sex|gender)[\s-]+(?:group|disaggregat\w*|breakdown)",
                          _last_user_text(state), re.I):
        term = (m_.group(1) or m_.group(2) or "").lower()
        if term and term not in [str(d).lower() for d in raw_disaggs]:
            raw_disaggs.append(term)
    resolved_disaggregations = []
    for d in raw_disaggs:
        if d == "ou_children":
            resolved_disaggregations.append(d)
            continue
        de_cat = None
        try:
            de_cat = await _category_from_data_elements(str(d), state, settings)
        except Exception as exc:
            logger.warning("de_category_lookup_failed", extra={"term": d, "error": str(exc)})
        if de_cat == "NOT_APPLICABLE":
            state.setdefault("resolution_notes", []).append(
                f"The selected data item can't be broken down by {d} in this DHIS2 instance, so totals are shown."
            )
            continue
        if de_cat:
            if de_cat["uid"] not in resolved_disaggregations:
                resolved_disaggregations.append(de_cat["uid"])
                state.setdefault("category_labels", {})[de_cat["uid"]] = de_cat["label"]
            logger.info("category_resolved_from_de", extra={"term": d, "uid": de_cat["uid"], "label": de_cat["label"]})
            continue
        if True:
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


# Below this, a match shares only one word with the request (keyword cascade
# strategy 5 = 0.72) or the LLM re-ranker rejected every candidate.
_WEAK_MATCH_CONFIDENCE = 0.75


async def _weak_match_question(weak: list[dict], settings: Settings) -> str:
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session

    parts = []
    try:
        async with get_db_session() as session:
            for m in weak:
                cands = await search_indicator_by_name(
                    session, m.get("label", ""), is_postgres=settings.is_postgres, limit=4
                )
                names = [c.get("label") for c in cands if c.get("label")]
                parts.append((m.get("label", "that"), names))
    except Exception:
        parts = [(m.get("label", "that"), []) for m in weak]
    lines = []
    for label, names in parts:
        if names:
            lines.append(f"I couldn't find a close match for \u201c{label}\u201d. The nearest items in this DHIS2 instance are: " + "; ".join(names) + ".")
        else:
            lines.append(f"I couldn't find \u201c{label}\u201d in this DHIS2 instance.")
    return " ".join(lines) + " Which one do you mean? You can also type the exact indicator or data element name."


_REPORTING_RE = re.compile(
    r"\b(reporting\s+rates?|report(?:ing)?\s+completeness|completeness|timeliness|"
    r"reports?\s+(?:received|submitted|on\s+time))\b",
    re.I,
)


async def _reporting_rate_metric(message: str, settings: Settings, token: str | None = None) -> dict | None:
    """Map 'reporting rate / completeness / timeliness of <data set>' to a dx item.

    Returns None when the message is not about reporting, a metric dict
    (object_type 'reportingRate', uid '<dataSet>.REPORTING_RATE[_ON_TIME]') when
    a data set name is found in the message, or {"clarify": ...} listing data
    sets when none is named.
    """
    if not message or not _REPORTING_RE.search(message):
        return None
    from backend.app.dhis2.client import DHIS2Client

    client = DHIS2Client(settings, token=token)
    raw = await client.get("/api/dataSets.json", params={"fields": "id,name", "paging": "false"})
    data_sets = [d for d in (raw.get("dataSets") or []) if d.get("id") and d.get("name")]
    low = " ".join(message.lower().split())
    matches = [
        d for d in data_sets
        if re.search(r"(?<![a-z0-9])" + re.escape(" ".join(d["name"].lower().split())) + r"(?![a-z0-9])", low)
    ]
    if not matches:
        names = ", ".join(sorted(d["name"] for d in data_sets)[:12])
        return {"clarify": f"Which data set's reporting rate do you mean? For example: {names}."}
    ds = max(matches, key=lambda d: len(d["name"]))
    on_time = bool(re.search(r"\b(timeliness|on\s+time|timely)\b", low))
    suffix = "REPORTING_RATE_ON_TIME" if on_time else "REPORTING_RATE"
    return {
        "label": f"{ds['name']} - Reporting rate{' on time' if on_time else ''}",
        "uid": f"{ds['id']}.{suffix}",
        "uid_confidence": 0.99,
        "object_type": "reportingRate",
    }


async def _exact_names_in_message(message: str, settings: Settings) -> list[dict]:
    """Indicator / data element / program indicator names quoted verbatim in the message.

    Whole-word, case-insensitive, at least 6 characters; names contained in a
    longer matched name are dropped ("ANC 1" inside "ANC 1 Coverage"). Returned
    in the order they appear in the message.
    """
    from sqlalchemy import text
    from backend.app.db.session import get_db_session

    msg = " ".join(message.split())
    if len(msg) < 6:
        return []
    table = "metadata_index" if settings.is_postgres else "metadata_index_fts"
    where_contains = (
        "POSITION(LOWER(name) IN LOWER(:msg)) > 0" if settings.is_postgres else "INSTR(LOWER(:msg), LOWER(name)) > 0"
    )
    async with get_db_session() as session:
        rows = (await session.execute(
            text(f"""
                SELECT uid, name, object_type FROM {table}
                WHERE object_type IN ('indicator', 'dataElement', 'programIndicator')
                  AND LENGTH(name) >= 6 AND {where_contains}
            """),
            {"msg": msg},
        )).fetchall()
    found: dict[str, dict] = {}
    low = msg.lower()
    for uid, name, obj_type in rows:
        n = " ".join(str(name).split())
        m = re.search(r"(?<![A-Za-z0-9])" + re.escape(n.lower()) + r"(?![A-Za-z0-9])", low)
        if m and n.lower() not in found:
            found[n.lower()] = {"uid": uid, "name": n, "object_type": obj_type, "pos": m.start()}
    names = list(found)
    keep = [v for k, v in found.items() if not any(k != o and k in o for o in names)]
    keep.sort(key=lambda v: v["pos"])
    return keep


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


async def _resolve_indicators_via_live_dhis2_api(
    metrics: list[dict],
    settings: Settings,
    state: AgentState,
    threshold: float,
) -> list[dict]:
    """Fallback: query live DHIS2 API directly when local metadata_index misses.

    Prevents the agent from failing with INDICATORS_NOT_FOUND whenever local
    metadata_index is unseeded or incomplete.
    """
    token = state.get("_dhis2_token")
    if not settings.dhis2_service_account_user and not token:
        return metrics

    try:
        from backend.app.dhis2.client import DHIS2Client
        client = DHIS2Client(settings, token=token)
    except Exception as exc:
        logger.warning("dhis2_client_init_failed_in_live_resolver", extra={"error": str(exc)})
        return metrics

    search_alts = state.get("metric_search_terms") or []

    resolved = []
    for metric in metrics:
        if metric.get("uid") and metric["uid"] != "UNRESOLVED" and metric.get("uid_confidence", 0) >= threshold:
            resolved.append(metric)
            continue

        search_candidates = [metric["label"]] + list(metric.get("search_terms") or []) + list(search_alts)
        found_match = None

        for query in search_candidates:
            if not query or not str(query).strip():
                continue
            query_str = str(query).strip()

            # Try /api/indicators
            try:
                raw_ind = await client.get(
                    "/api/indicators.json",
                    params={"filter": f"name:ilike:{query_str}", "fields": "id,name", "pageSize": "5"},
                )
                items = raw_ind.get("indicators", [])
                if items:
                    found_match = {
                        "label": items[0]["name"],
                        "uid": items[0]["id"],
                        "uid_confidence": 0.88,
                        "object_type": "indicator",
                    }
                    break
            except Exception as exc:
                logger.debug("live_dhis2_indicator_search_failed", extra={"query": query_str, "error": str(exc)})

            # Try /api/dataElements
            try:
                raw_de = await client.get(
                    "/api/dataElements.json",
                    params={"filter": f"name:ilike:{query_str}", "fields": "id,name", "pageSize": "5"},
                )
                items = raw_de.get("dataElements", [])
                if items:
                    found_match = {
                        "label": items[0]["name"],
                        "uid": items[0]["id"],
                        "uid_confidence": 0.88,
                        "object_type": "dataElement",
                    }
                    break
            except Exception as exc:
                logger.debug("live_dhis2_dataelement_search_failed", extra={"query": query_str, "error": str(exc)})

            # Try /api/programIndicators
            try:
                raw_pi = await client.get(
                    "/api/programIndicators.json",
                    params={"filter": f"name:ilike:{query_str}", "fields": "id,name", "pageSize": "5"},
                )
                items = raw_pi.get("programIndicators", [])
                if items:
                    found_match = {
                        "label": items[0]["name"],
                        "uid": items[0]["id"],
                        "uid_confidence": 0.88,
                        "object_type": "programIndicator",
                    }
                    break
            except Exception as exc:
                logger.debug("live_dhis2_progindicator_search_failed", extra={"query": query_str, "error": str(exc)})

        if found_match:
            logger.info("live_dhis2_metadata_resolved", extra={"query": metric["label"], "resolved": found_match})
            resolved.append(found_match)
        else:
            resolved.append(metric)

    return resolved


async def _resolve_org_unit_from_db_or_live_api(
    label: str,
    settings: Settings,
    token: str | None = None,
) -> dict | None:
    # 1. Try local DB first
    try:
        resolved = await _resolve_org_unit_from_db(label, settings)
        if resolved:
            return resolved
    except Exception as exc:
        logger.warning("local_org_unit_db_search_failed", extra={"label": label, "error": str(exc)})

    # 1b. Misspellings ("Kenama", "Bombli"): closest org unit name in the index.
    try:
        fuzzy = await _fuzzy_org_unit(label, settings)
        if fuzzy:
            return fuzzy
    except Exception as exc:
        logger.warning("fuzzy_org_unit_failed", extra={"label": label, "error": str(exc)})

    # 2. Try live DHIS2 API
    if settings.dhis2_service_account_user or token:
        try:
            from backend.app.dhis2.client import DHIS2Client
            client = DHIS2Client(settings, token=token)
            raw = await client.get(
                "/api/organisationUnits.json",
                params={"filter": f"name:ilike:{label.strip()}", "fields": "id,name,level", "pageSize": "3"},
            )
            units = raw.get("organisationUnits", [])
            if units:
                best = units[0]
                logger.info("live_dhis2_org_unit_resolved", extra={"label": label, "resolved_uid": best["id"], "resolved_name": best["name"]})
                return {
                    "label": best["name"],
                    "uid": best["id"],
                    "level": best.get("level", 2),
                }
        except Exception as exc:
            logger.warning("live_dhis2_org_unit_search_failed", extra={"label": label, "error": str(exc)})

    return None


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


# ---------------------------------------------------------------------------
# Breakdown level + expert metric resolution
# ---------------------------------------------------------------------------

def _last_user_text(state: AgentState) -> str:
    for msg in reversed(state.get("messages") or []):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _context_messages(state: AgentState) -> list[str]:
    """Earlier turns (user questions and the last assistant reply) for follow-ups."""
    msgs = state.get("messages") or []
    out: list[str] = []
    for msg in msgs[:-1][-4:]:
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        role = "assistant" if msg.get("role") == "assistant" else "user"
        out.append(f"{role}: {content[:300]}")
    return out


async def _apply_breakdown_level(state: AgentState, settings: Settings) -> None:
    from backend.app.agent.resolution import org_levels, parse_breakdown_level, level_name, _plural as _p

    org = state.get("org_unit") or {}
    levels = await org_levels(settings, token=state.get("_dhis2_token"))
    label_low = (org.get("label") or "").strip().lower()
    level_words = {l["name"].lower() for l in levels or []} | {_p(l["name"].lower()) for l in levels or []}
    if label_low in level_words and not _looks_like_dhis2_uid(org.get("uid") or ""):
        # The parser took "district" in "by district" for a place name.
        org["label"] = "National"
        org["uid"] = "UNRESOLVED"
    org_level = int(org.get("level") or 1)
    if (org.get("label") or "").lower() in {"national", "federal"} or not _looks_like_dhis2_uid(org.get("uid") or ""):
        # National, or a place that did not resolve: analytics uses the user's
        # top org unit (USER_ORGUNIT), so breakdowns count from level 1.
        org_level = 1
        org["level"] = 1
    disaggs = list(state.get("disaggregations") or [])
    wanted = parse_breakdown_level(_last_user_text(state), levels)
    if wanted is None and "ou_children" in disaggs:
        wanted = org_level + 1
    if wanted is not None and wanted > org_level:
        org["breakdown_level"] = wanted
        org["breakdown_level_name"] = level_name(wanted, levels)
        if "ou_children" not in disaggs:
            disaggs.append("ou_children")
    elif wanted is not None and wanted <= org_level and "ou_children" in disaggs:
        # e.g. "by district" when the place asked for already is a district.
        disaggs = [d for d in disaggs if d != "ou_children"]
    org["level_name"] = level_name(org_level, levels)
    state["org_unit"] = org
    state["disaggregations"] = disaggs


def _period_text(state: AgentState) -> str:
    return ", ".join(state.get("periods") or []) or "THIS_YEAR"


def _place_text(state: AgentState) -> str:
    org = state.get("org_unit") or {}
    label = org.get("label") or "National"
    if label.lower() == "national":
        label = "whole country (national)"
    bl = org.get("breakdown_level_name")
    return f"{label}, broken down by {bl}" if bl else label


async def _expert_resolve(state: AgentState, settings: Settings) -> dict:
    """Resolve every not-yet-certain metric with rank -> probe -> judge.

    Returns {"status": "ok" | "skip" | "clarify" | "not_available", ...}.
    "skip" means the catalogue is unavailable and the legacy cascade runs.
    """
    from backend.app.agent import resolution as R
    from backend.app.dhis2.analytics import ou_dimension

    metrics = state.get("metrics") or []
    cat = await R.load_catalogue(settings)
    if not cat["items"]:
        return {"status": "skip"}

    user_text = _last_user_text(state)
    org = state.get("org_unit") or {}
    # Only strip the place name from the question when it resolved to a real org
    # unit; a mis-parsed "place" (e.g. the indicator name) must not be erased.
    org_stop = (
        set(R.tokens(org.get("label") or "")) | set(R.tokens(org.get("original_label") or ""))
        if _looks_like_dhis2_uid(org.get("uid") or "") else set()
    )
    q_user = [
        w for w in R.content_tokens(user_text, org_stop)
        if not (w in cat["ou_tokens"] and w not in cat["vocab"])
    ]

    def exact_is_best(m: dict) -> bool:
        """A typed exact name is trusted only if no other item fits the user's words better
        ("BCG doses were given" should not lock onto "BCG doses" over "BCG doses given")."""
        if not q_user:
            return True
        by_uid = {it["uid"]: it for it in cat["items"]}
        it = by_uid.get(m.get("uid"))
        if it is None:
            return False
        mine = R.score_item(q_user, it, cat)
        top = R.rank_candidates([(q_user, 1.0)], cat, limit=1)
        return not top or top[0]["uid"] == it["uid"] or top[0]["score"] <= mine + 0.05

    todo = [
        i for i, m in enumerate(metrics)
        if not (m.get("uid_confidence", 0) >= 0.97 and _looks_like_dhis2_uid(m.get("uid") or "")
                and exact_is_best(m))
    ]
    if not todo:
        return {"status": "ok"}
    unknown = R.unknown_tokens(q_user, cat)

    client = None
    if settings.dhis2_service_account_user or state.get("_dhis2_token"):
        from backend.app.dhis2.client import DHIS2Client
        client = DHIS2Client(settings, token=state.get("_dhis2_token"))
    ou = ou_dimension(state)
    pe = ";".join(state.get("periods") or ["THIS_YEAR"])
    use_llm = settings.llm_provider != "mock"
    context = _context_messages(state)

    async def one(i: int) -> dict:
        m = metrics[i]
        label = str(m.get("label") or "")
        q_label = R.content_tokens(label, org_stop)
        queries = [(q_user, 1.0), (q_label, 0.95)]
        for t in (m.get("search_terms") or [])[:4]:
            queries.append((R.content_tokens(str(t), org_stop), 0.85))
        pool = R.rank_candidates(queries, cat, limit=40)
        for c in pool:
            c["user_score"] = R.score_item(q_user, {"tokens": {*R.tokens(c["label"])},
                                                     "name_tokens": R.tokens(c["label"])}, cat) if q_user else c["score"]
        has = None
        if client is not None and pool:
            has = await R.probe_data(client, [c["uid"] for c in pool], ou, pe)
        for c in pool:
            c["has_data"] = None if has is None else (has.get(c["uid"]) is not None)
            if has is not None and has.get(c["uid"]) is not None:
                c["data_total"] = has[c["uid"]]
        if has is None:
            shown = R.diversify(pool, 12)
        else:
            with_data = R.diversify([c for c in pool if c["has_data"]], 9)
            without = [c for c in pool if not c["has_data"]][: max(3, 12 - len(with_data))]
            shown = sorted(with_data + without, key=lambda c: -c["score"])[:12]
        verdict = None
        if R.is_vague(q_user) and not context:
            # "Show me coverage": nothing in the user's words picks a measure.
            # Ask, whatever the parser guessed; options come from the user's words.
            vague_pool = R.rank_candidates([(q_user, 1.0)], cat, limit=40) if q_user else []
            for c in vague_pool:
                c["has_data"] = None if (has is None or c["uid"] not in has) else (has[c["uid"]] is not None)
            opts = R.diversify([c for c in vague_pool if c["has_data"] is not False] or vague_pool, 12, keep_top=1)
            return {"decision": "ambiguous", "choice": 0, "alternatives": [], "options": [],
                    "candidates": opts or shown, "label": label, "by": "vague",
                    "requested_concept": " ".join(user_text.split()[:6]) if not q_user else " ".join(q_user)}
        if use_llm and shown:
            await R.annotate_program_names(client, shown)
            verdict = await R.llm_judge(user_text, label, shown, _place_text(state), _period_text(state),
                                        context, settings)
        if verdict is None:
            rescored = [{**c, "score": c["user_score"] if q_user else c["score"]} for c in shown]
            verdict = R.deterministic_judge(q_user, rescored, unknown)
        verdict["candidates"] = shown
        verdict["label"] = label
        if verdict["decision"] == "match":
            _prefer_aggregate(verdict, user_text, q_user, cat, R)
        return verdict

    verdicts = await asyncio.gather(*(one(i) for i in todo))

    ambiguous = [v for v in verdicts if v["decision"] == "ambiguous"]
    missing = [v for v in verdicts if v["decision"] == "not_available"]
    if ambiguous:
        return {"status": "clarify", "question": _ambiguous_question(ambiguous[0])}
    if missing and len(missing) == len(metrics):
        return {"status": "not_available", "message": _not_available_message(missing, state)}

    notes: list[str] = []
    new_metrics = list(metrics)
    drop: set[int] = set()
    for i, v in zip(todo, verdicts):
        if v["decision"] == "not_available":
            drop.add(i)
            notes.append(f"“{v.get('requested_concept') or v['label']}” is not available in this DHIS2 instance, so it is left out.")
            continue
        cands = v["candidates"]
        chosen = cands[v["choice"] - 1]
        alts = [cands[j - 1] for j in v.get("alternatives") or [] if cands[j - 1].get("has_data") is not False]
        new_metrics[i] = {
            **{k: val for k, val in metrics[i].items() if k not in {"search_terms"}},
            "label": chosen["label"],
            "uid": chosen["uid"],
            "uid_confidence": 0.95,
            "object_type": chosen["object_type"],
            "requested_label": metrics[i].get("label"),
            "resolved_by": v.get("by"),
            "has_data": chosen.get("has_data"),
            "fallbacks": [
                {"label": a["label"], "uid": a["uid"], "object_type": a["object_type"]} for a in alts[:3]
            ],
        }
        logger.info("expert_resolved", extra={
            "requested": metrics[i].get("label"), "chosen": chosen["label"], "uid": chosen["uid"],
            "by": v.get("by"), "has_data": chosen.get("has_data"), "reason": v.get("reason"),
        })
    state["metrics"] = [m for j, m in enumerate(new_metrics) if j not in drop]
    if notes:
        state.setdefault("resolution_notes", []).extend(notes)
    return {"status": "ok"}


def _option_line(c: dict) -> str:
    from backend.app.agent.resolution import TYPE_LABEL
    tail = " (no data for this place and period)" if c.get("has_data") is False else ""
    return f"{c['label']} ({TYPE_LABEL.get(c['object_type'], c['object_type'])}){tail}"


def _pick_options(v: dict, n: int = 5) -> list[dict]:
    cands = v.get("candidates") or []
    idx = [j for j in (v.get("options") or []) if 1 <= j <= len(cands)]
    if len(idx) < 3:
        from backend.app.agent.resolution import tokens
        ranked = sorted(range(1, len(cands) + 1),
                        key=lambda j: (-(cands[j - 1].get("has_data") is True), -cands[j - 1]["score"]))
        firsts = {(tokens(cands[j - 1]["label"]) or [""])[0] for j in idx}
        # First pass: one option per name family; second pass fills up.
        for pass_ in (0, 1):
            for j in ranked:
                if len(idx) >= n:
                    break
                first = (tokens(cands[j - 1]["label"]) or [""])[0]
                if j in idx or (pass_ == 0 and first in firsts):
                    continue
                idx.append(j)
                firsts.add(first)
    return [cands[j - 1] for j in idx[:n]]


def _ambiguous_question(v: dict) -> str:
    concept = v.get("requested_concept") or v.get("label") or "that"
    opts = _pick_options(v, 5)
    lines = "\n".join(f"{k}. {_option_line(c)}" for k, c in enumerate(opts, 1))
    if v.get("source_conflict"):
        return (
            f"“{concept}” exists in this DHIS2 instance both as routine aggregate data and as tracker "
            f"(individual-record) programme data, and they give different numbers. Which source do you want?\n\n"
            f"{lines}\n\nReply with the name (or number), and I'll run it."
        )
    return (
        f"“{concept}” could mean several different measures in this DHIS2 instance. Which one do you want?\n\n"
        f"{lines}\n\nReply with the name (or number), and I'll run it."
    )


def _not_available_message(missing: list[dict], state: AgentState) -> str:
    parts = []
    for v in missing:
        concept = v.get("requested_concept") or v.get("label") or "That"
        opts = _pick_options(v, 4)
        msg = (
            f"**{concept}** isn't available in this DHIS2 instance: no data element, indicator or "
            f"program indicator here measures it, so I haven't substituted anything else."
        )
        if opts:
            msg += "\n\nClosest items that do exist here:\n" + "\n".join(f"- {_option_line(c)}" for c in opts)
        parts.append(msg)
    return "\n\n".join(parts) + "\n\nAsk about one of those, or type the exact name of the data item you need."


_CAT_SYNONYMS = {
    "sex": {"sex", "gender", "male", "female"},
    "gender": {"sex", "gender", "male", "female"},
    "age": {"age", "year", "month", "under", "child", "paediatric"},
    "age group": {"age", "year", "month", "under", "paediatric"},
}
_DE_CATS_CACHE: dict[str, list[dict]] = {}


async def _category_from_data_elements(term: str, state: AgentState, settings: Settings):
    """Category of the selected data element(s) that matches a breakdown word.

    Returns {"uid","label"}, "NOT_APPLICABLE" when the selected items are
    indicators / program indicators or have no such category, or None when the
    lookup could not run (legacy search then takes over).
    """
    from backend.app.agent.resolution import tokens

    metrics = [m for m in state.get("metrics") or [] if _looks_like_dhis2_uid(m.get("uid") or "")]
    if not metrics:
        return None
    want = _CAT_SYNONYMS.get(term.lower().strip(), set(tokens(term)))
    des = [m for m in metrics if m.get("object_type") == "dataElement"]
    if not des:
        return "NOT_APPLICABLE"
    if not (settings.dhis2_service_account_user or state.get("_dhis2_token")):
        return None
    from backend.app.dhis2.client import DHIS2Client
    client = DHIS2Client(settings, token=state.get("_dhis2_token"))
    for m in des:
        uid = m["uid"]
        if uid not in _DE_CATS_CACHE:
            raw = await client.get(f"/api/dataElements/{uid}.json",
                                   params={"fields": "categoryCombo[categories[id,name]]"})
            _DE_CATS_CACHE[uid] = list(((raw.get("categoryCombo") or {}).get("categories")) or [])
        for c in _DE_CATS_CACHE[uid]:
            if c.get("name", "").lower() == "default":
                continue
            if set(tokens(c.get("name", ""))) & want:
                return {"uid": c["id"], "label": c["name"]}
    return "NOT_APPLICABLE"


async def _fuzzy_org_unit(label: str, settings: Settings) -> dict | None:
    """Closest org unit by spelling (difflib ratio >= 0.8), shallower level first."""
    import difflib
    from sqlalchemy import text as sa_text
    from backend.app.db.session import get_db_session

    name = re.sub(r"\b(district|chiefdom|facility|province|region)\b", "", label, flags=re.I).strip().lower()
    if len(name) < 3:
        return None
    table = "metadata_index" if settings.is_postgres else "metadata_index_fts"
    async with get_db_session() as session:
        rows = (await session.execute(sa_text(
            f"SELECT uid, name, raw_metadata FROM {table} WHERE object_type = 'orgUnit'"
        ))).fetchall()
    best = None
    for uid, n, raw in rows:
        nm = " ".join(str(n or "").split())
        r = difflib.SequenceMatcher(None, name, nm.lower()).ratio()
        if r < 0.8:
            continue
        level = 9
        try:
            level = int((json.loads(raw) if isinstance(raw, str) else (raw or {})).get("level", 9))
        except Exception:
            pass
        key = (r, -level)
        if best is None or key > best[0]:
            best = (key, {"label": nm, "uid": uid, "level": level if level != 9 else 2})
    if best:
        logger.info("org_unit_fuzzy_resolved", extra={"label": label, "resolved": best[1]["label"]})
        return best[1]
    return None


def _prefer_aggregate(verdict: dict, user_text: str, q_user: list[str], cat: dict, R) -> None:
    """Routine (aggregate) data is the default answer in DHIS2.

    If a tracker program indicator was chosen although the user never mentioned
    a programme/tracker/case-based source, and the judge itself listed an
    aggregate data element/indicator with data as measuring the same concept,
    use the aggregate one (the program indicator stays as an alternate).
    """
    cands = verdict["candidates"]
    chosen = cands[verdict["choice"] - 1]
    if chosen.get("object_type") != "programIndicator" or R.mentions_tracker(user_text):
        return
    for j in verdict.get("alternatives") or []:
        c = cands[j - 1]
        if c.get("object_type") in ("dataElement", "indicator") and c.get("has_data"):
            old_choice = verdict["choice"]
            verdict["choice"] = j
            verdict["alternatives"] = [old_choice] + [k for k in verdict["alternatives"] if k not in (j, old_choice)]
            verdict["by"] = f"{verdict.get('by')}+aggregate"
            return


async def _indicator_formulas(inds: list[dict], state: AgentState, settings: Settings) -> None:
    """Attach numerator/denominator descriptions and the indicator type to each indicator."""
    if not (settings.dhis2_service_account_user or state.get("_dhis2_token")):
        return
    from backend.app.dhis2.client import DHIS2Client
    client = DHIS2Client(settings, token=state.get("_dhis2_token"))
    for m in inds:
        raw = await client.get(f"/api/indicators/{m['uid']}.json", params={
            "fields": "numeratorDescription,denominatorDescription,indicatorType[name,factor]"})
        m["formula"] = {
            "numerator": raw.get("numeratorDescription") or "",
            "denominator": raw.get("denominatorDescription") or "",
            "type": (raw.get("indicatorType") or {}).get("name") or "",
            "factor": (raw.get("indicatorType") or {}).get("factor"),
        }
