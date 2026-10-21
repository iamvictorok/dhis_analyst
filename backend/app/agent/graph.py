"""LangGraph state machine — streaming agent pipeline.

Architecture:
  classify_intent → resolve_metadata → fetch_dhis2 → enrich_web →
  evidence_fusion → generate_content → route_to_renderer → SSE stream

The chat endpoint calls run_agent_stream() which yields typed SSE events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable

from backend.config import Settings
from backend.app.agent.intent import classify_intent_llm
from backend.app.agent.map_builder import MapUnavailable, build_map, export_payload_from_map
from backend.app.agent.nodes.enrich_web import enrich_web
from backend.app.agent.nodes.evidence_fusion import fuse_evidence
from backend.app.agent.nodes.fetch_dhis2 import fetch_dhis2
from backend.app.agent.nodes.generate_content import generate_content
from backend.app.agent.nodes.metadata_resolve import resolve_metadata

from backend.app.agent.renderers.export import render_export_payload
from backend.app.agent.state import AgentState
from backend.app.models import ChatRequest, Identity

logger = logging.getLogger("dhis2_analyst.graph")


class _NodeTimes:
    """Context-local list of (node, seconds) for the current request."""
    import contextvars as _cv
    _var = _cv.ContextVar("node_times", default=None)

    def begin(self) -> list:
        lst: list = []
        self._var.set(lst)
        return lst

    def append(self, item) -> None:
        lst = self._var.get()
        if lst is not None:
            lst.append(item)


_NODE_TIMES = _NodeTimes()


def _timings_event(start: float, nodes: list, calls: list) -> str:
    return sse("timings", {
        "total_seconds": round(time.monotonic() - start, 2),
        "nodes": [{"node": n, "seconds": t} for n, t in nodes],
        "llm_calls": calls,
    })


def sse(event: str, payload: dict | list | str | None = None) -> str:
    body = json.dumps(_json_ready(payload if payload is not None else {}), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def _json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(v) for v in value]
    return value


async def _timed_node(
    name: str,
    fn: Callable[[AgentState, Settings], Awaitable[AgentState]],
    state: AgentState,
    settings: Settings,
) -> AgentState:
    start = time.monotonic()
    try:
        return await fn(state, settings)
    finally:
        _NODE_TIMES.append((name, round(time.monotonic() - start, 2)))
        logger.info(
            "agent_node_timing",
            extra={
                "node": name,
                "session_id": state.get("session_id"),
                "duration_ms": int((time.monotonic() - start) * 1000),
            },
        )


async def build_initial_state(
    request: ChatRequest,
    settings: Settings,
    identity: Identity,
    history: list[dict] | None = None,
    dhis2_token: str | None = None,
) -> AgentState:
    """Classify intent (LLM-assisted when real provider is configured)."""
    intent = await classify_intent_llm(
        request.message,
        settings,
        forced_mode=request.output_mode,
        history=history,
    )
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": request.message})

    return {
        "messages": messages,
        "session_id": request.session_id or str(uuid.uuid4()),
        "user_id": identity.user_id,
        "user_role": identity.role,
        "output_mode": intent["output_mode"],
        "metrics": intent["metrics"],
        "metric_search_terms": intent.get("metric_search_terms", []),
        "org_unit": intent["org_unit"],
        "periods": intent["periods"],
        "disaggregations": intent["disaggregations"],
        "viz_types": intent["viz_types"],
        "needs_web_enrichment": bool(intent["needs_web_enrichment"] and request.allow_web),
        "web_search_queries": intent["web_search_queries"],
        "data_retrieval_strategy": intent["data_retrieval_strategy"],
        "clarification_needed": intent["clarification_needed"],
        "clarification_question": intent["clarification_question"],
        "requires_data": intent.get("requires_data", True),
        "query_expansion": intent.get("query_expansion", ""),
        "dhis2_data": {},
        "pii_map": {},
        "web_context": [],
        "evidence_items": [],
        "active_report_html": "",
        "active_chart_configs": [],
        "active_slide_manifest": [],
        "generated_file_id": None,
        "active_conversational_response": None,
        "fetch_error": None,
        "_dhis2_token": dhis2_token,
    }


async def run_agent_stream(
    request: ChatRequest,
    settings: Settings,
    identity: Identity | None = None,
    history: list[dict] | None = None,
    dhis2_token: str | None = None,
) -> AsyncIterator[str]:
    """Main agent pipeline — yields SSE-formatted strings."""
    if identity is None:
        from backend.app.models import Identity as _Identity
        identity = _Identity(user_id="anonymous")

    logger.info(
        "agent_start",
        extra={
            "session_id": request.session_id,
            "user_id": identity.user_id,
            "message_len": len(request.message),
        },
    )
    pipeline_start = time.monotonic()
    from backend.app.llm import begin_call_log
    _llm_calls = begin_call_log()
    _nodes = _NODE_TIMES.begin()
    from backend.app.dhis2 import failover
    source_tracker = failover.begin_request()

    _t_intent = time.monotonic()
    try:
        state = await build_initial_state(request, settings, identity, history, dhis2_token)
        _NODE_TIMES.append(("classify_intent", round(time.monotonic() - _t_intent, 2)))
    except Exception as exc:
        logger.error("intent_classification_failed", extra={"error": str(exc)}, exc_info=True)
        error_str = str(exc).lower()
        if "timeout" in error_str:
            msg = "The AI provider timed out while processing your request. Please try again."
        elif "rate limit" in error_str or "429" in error_str:
            msg = "The AI provider is currently rate limited. Please wait a moment and try again."
        else:
            msg = "Could not understand your request due to an AI parsing error. Please try rephrasing."
        
        yield sse("error", {"code": "INTENT_FAILED", "user_message": msg, "detail": str(exc)})
        yield _timings_event(pipeline_start, _nodes, _llm_calls)
        yield sse("done")
        return

    # Map mode is decided by the intent rules (explicit map / "where" cue only).
    map_requested = state["output_mode"] == "map"
    map_payload: dict | None = None
    map_note = ""

    # --- Clarification gate ---
    if state["clarification_needed"]:
        yield sse("clarification", {"question": state["clarification_question"]})
        yield _timings_event(pipeline_start, _nodes, _llm_calls)
        yield sse("done")
        return

    # --- Data pipeline (skipped for casual chitchat) ---
    if state["requires_data"]:
        # --- Metadata resolution ---
        try:
            state = await _timed_node("resolve_metadata", resolve_metadata, state, settings)
        except Exception as exc:
            logger.error("metadata_resolve_failed", extra={"error": str(exc)})
            yield sse("error", {"code": "METADATA_FAILED", "user_message": "Could not resolve indicator metadata.", "detail": str(exc)})
            yield _timings_event(pipeline_start, _nodes, _llm_calls)
            yield sse("done")
            return

        if state["clarification_needed"]:
            yield sse("clarification", {"question": state["clarification_question"]})
            yield _timings_event(pipeline_start, _nodes, _llm_calls)
            yield sse("done")
            return

        # --- Requested concept is not in this instance: say so, never substitute ---
        if state.get("not_available_message"):
            logger.info("concept_not_available", extra={"session_id": state["session_id"]})
            for chunk in _chunk(state["not_available_message"], 64):
                yield sse("token", {"text": chunk})
            yield _timings_event(pipeline_start, _nodes, _llm_calls)
            yield sse("done")
            return

        # --- Pre-flight: check for UNRESOLVED metrics ---
        unresolved = [
            m for m in state["metrics"]
            if m.get("uid") == "UNRESOLVED" or not m.get("uid")
        ]
        if unresolved and len(unresolved) == len(state["metrics"]):
            labels = [m.get("label", "?") for m in unresolved]
            logger.warning(
                "all_metrics_unresolved",
                extra={"labels": labels, "session_id": state["session_id"]},
            )
            friendly = (
                f"I couldn't find any health indicators matching your request "
                f"({', '.join(labels)}) in this DHIS2 instance. "
                f"Try using more specific health terminology (e.g. 'Malaria confirmed cases', "
                f"'ANC 1st visit', 'OPV3 doses given')."
            )
            yield sse("error", {
                "code": "INDICATORS_NOT_FOUND",
                "user_message": friendly,
            })
            yield _timings_event(pipeline_start, _nodes, _llm_calls)
            yield sse("done")
            return

        # --- Data retrieval ---
        try:
            state = await _timed_node("fetch_dhis2", fetch_dhis2, state, settings)
        except Exception as exc:
            if hasattr(exc, "response") and hasattr(exc.response, "text"):
                error_msg = f"HTTP {exc.response.status_code} - {exc.response.text}"
            else:
                error_msg = str(exc)
            is_period_error = "409" in error_msg and "period" in error_msg.lower()
            is_dimension_error = "409" in error_msg and ("dimension" in error_msg.lower() or "aggregation" in error_msg.lower())

            if is_period_error:
                # Retry with a safe fallback period
                logger.warning(
                    "fetch_dhis2_period_retry",
                    extra={
                        "original_periods": state.get("periods"),
                        "fallback_period": "THIS_YEAR",
                    },
                )
                state["periods"] = ["THIS_YEAR"]
                try:
                    state = await _timed_node("fetch_dhis2_retry", fetch_dhis2, state, settings)
                except Exception as retry_exc:
                    logger.error("fetch_dhis2_retry_failed", extra={"error": str(retry_exc)})
                    friendly_error = _user_friendly_error(str(retry_exc))
                    state["fetch_error"] = friendly_error
                    yield sse("error", {
                        "code": "DATA_FETCH_FAILED",
                        "user_message": f"Could not retrieve DHIS2 data even with fallback period: {friendly_error}",
                        "detail": str(retry_exc),
                    })
                    # Non-fatal — continue with empty data
            elif is_dimension_error and (state.get("disaggregations") or len(state.get("metrics", [])) > 1):
                logger.warning(
                    "fetch_dhis2_dimension_retry",
                    extra={
                        "original_disaggregations": state.get("disaggregations"),
                        "original_metrics_count": len(state.get("metrics", [])),
                        "error": error_msg,
                    }
                )
                # First try to regex out the exact failing UID from the error
                uid_match = re.search(r"\b[a-zA-Z][a-zA-Z0-9]{10}\b", error_msg)
                stripped_uid = False
                if uid_match:
                    failing_uid = uid_match.group(0)
                    metrics = state.get("metrics", [])
                    if any(m.get("uid") == failing_uid for m in metrics):
                        state["metrics"] = [m for m in metrics if m.get("uid") != failing_uid]
                        stripped_uid = True
                        logger.info("precision_self_healing_dropped_uid", extra={"uid": failing_uid})
                        
                if not stripped_uid:
                    # First try stripping disaggregations (e.g. ou_children causing 409s)
                    state["disaggregations"] = []
                    # And trim metrics to the safest top hit
                    if state.get("metrics"):
                        state["metrics"] = state["metrics"][:1]
                
                try:
                    state = await _timed_node("fetch_dhis2_retry", fetch_dhis2, state, settings)
                except Exception as retry_exc:
                    logger.error("fetch_dhis2_retry_failed", extra={"error": str(retry_exc)})
                    friendly_error = _user_friendly_error(str(retry_exc))
                    state["fetch_error"] = friendly_error
                    yield sse("error", {
                        "code": "DATA_FETCH_FAILED",
                        "user_message": f"Could not retrieve DHIS2 data even after falling back to a safer query: {friendly_error}",
                        "detail": str(retry_exc),
                    })
                    # Non-fatal — continue with empty data
            else:
                logger.error(
                    "fetch_dhis2_failed", 
                    extra={
                        "error": error_msg,
                        "metrics": state.get("metrics"),
                        "periods": state.get("periods"),
                        "org_unit": state.get("org_unit")
                    },
                    exc_info=True
                )
                friendly_error = _user_friendly_error(error_msg)
                state["fetch_error"] = friendly_error
                yield sse("error", {
                    "code": "DATA_FETCH_FAILED",
                    "user_message": f"Could not retrieve DHIS2 data: {friendly_error}",
                    "detail": error_msg,
                    "suggestion": "Try rephrasing your request with different indicator names.",
                })
                # Non-fatal — continue with empty data

        # --- Empty result: try the next-best candidates for the same concept ---
        if not state.get("fetch_error") and not (state.get("dhis2_data") or {}).get("rows"):
            state = await _try_fallback_candidates(state, settings)

        # --- Web enrichment ---
        try:
            state = await _timed_node("enrich_web", enrich_web, state, settings)
        except Exception as exc:
            logger.warning("web_enrichment_failed", extra={"error": str(exc)})
            state["web_context"] = []  # Non-fatal

        # --- Evidence fusion ---
        try:
            state = await _timed_node("fuse_evidence", fuse_evidence, state, settings)
        except Exception as exc:
            logger.warning("evidence_fusion_failed", extra={"error": str(exc)})
            state["evidence_items"] = []

        if state["evidence_items"]:
            yield sse("evidence", state["evidence_items"])

        source_event = _data_source_event(source_tracker, settings)
        if source_event:
            yield sse("data_source", source_event)
    else:
        logger.info("skipping_data_pipeline", extra={"reason": "requires_data=False", "session_id": state.get("session_id")})

    # --- Empty data guard: refuse to hallucinate from nothing ---
    if state["requires_data"] and not state.get("dhis2_data", {}).get("rows"):
        mode = state["output_mode"]
        
        if state.get("fetch_error"):
            logger.info("agent_done_fetch_error", extra={"session_id": state["session_id"], "mode": mode})
            yield _timings_event(pipeline_start, _nodes, _llm_calls)
            yield sse("done")
            return
            
        if mode in ("report", "dashboard", "presentation", "export", "map"):
            data_used = _data_used_text(state)
            if data_used:
                yield sse("token", {"text": data_used + "\n\n"})
            yield sse("report_html", {"html": _no_data_html(state)})
            logger.info("agent_done_no_data", extra={"session_id": state["session_id"], "mode": mode})
            yield _timings_event(pipeline_start, _nodes, _llm_calls)
            yield sse("done")
            return

    # --- Map: values per area joined to DHIS2 geometry; honest fallback ---
    if map_requested and state["output_mode"] == "map":
        _t_map = time.monotonic()
        try:
            map_payload = await build_map(state, settings)
        except MapUnavailable as exc:
            map_note = f"I couldn't draw a map: {exc}. Showing a chart instead."
        except Exception as exc:  # never let the map break the answer
            logger.error("map_build_failed", extra={"error": str(exc)[:300]}, exc_info=True)
            map_note = "I couldn't draw a map because of an unexpected error. Showing a chart instead."
        finally:
            _NODE_TIMES.append(("build_map", round(time.monotonic() - _t_map, 2)))
        if map_payload is None:
            state["output_mode"] = "dashboard"
            state["viz_types"] = [v for v in state.get("viz_types", []) if v != "map"] or ["bar"]

    # --- Content generation + routing ---
    try:
        if map_payload is not None:
            pass  # the map payload is the content
        else:
            state = await _timed_node("generate_content", generate_content, state, settings)
    except Exception as exc:
        logger.error("content_generation_failed", extra={"error": str(exc)})
        yield sse("error", {"code": "CONTENT_FAILED", "user_message": "Content generation failed.", "detail": str(exc)})
        yield _timings_event(pipeline_start, _nodes, _llm_calls)
        yield sse("done")
        return

    mode = state["output_mode"]
    cached_label = None
    if source_tracker.get("mode") == "cached":
        cached_label = failover.snapshot_label(settings)

    data_used = _data_used_text(state) if state["requires_data"] else ""
    if data_used and mode != "conversational":
        yield sse("token", {"text": data_used + "\n\n"})
    if map_note:
        yield sse("token", {"text": map_note + "\n\n"})
    if cached_label and mode != "conversational":
        yield sse("token", {"text": f"⚠️ {cached_label} (DHIS2 demo snapshot — the live server is unreachable).\n\n"})

    if map_payload is not None:
        summary = _map_summary(map_payload)
        if summary:
            yield sse("token", {"text": summary + "\n\n"})
        map_payload["export"] = export_payload_from_map(map_payload)
        yield sse("map_config", map_payload)
    elif mode == "dashboard":
        charts = state["active_chart_configs"]
        if charts:
            for chart in charts:
                yield sse("chart_config", chart)
                await asyncio.sleep(0)
        else:
            # Data was retrieved but no chart could be built — don't end the
            # stream silently with just 'done' (blank assistant bubble).
            yield sse("token", {
                "text": "I retrieved the data but couldn't build a chart from it. "
                        "Try rephrasing your request or asking for a table or report.",
            })
    elif mode in ("report", "map"):
        html = state["active_report_html"]
        if cached_label:
            html = (
                '<p class="cached-banner"><strong>' + cached_label
                + '</strong> — DHIS2 demo snapshot; the live server is unreachable.</p>' + html
            )
        yield sse("report_html", {"html": html})
    elif mode == "presentation":
        yield sse("slide_manifest", state["active_slide_manifest"])
    elif mode == "export":
        payload = render_export_payload(state)
        if cached_label:
            payload["sources"] = [{
                "title": f"{cached_label} — DHIS2 demo snapshot (live server unreachable)",
                "url": "",
                "confidence": "",
            }] + list(payload.get("sources") or [])
            payload.setdefault("metadata", {})["data_source"] = "demo_snapshot"
        yield sse("data_ready", {
            "file_id": None,
            "format": "json",
            "row_count": len(payload.get("rows", [])),
            "data": payload,
        })
    else:
        # Conversational — stream token by token
        answer = state.get("active_conversational_response") or ""
        if data_used:
            answer = answer.rstrip() + "\n\n" + data_used
        if cached_label:
            answer = f"_⚠️ {cached_label} (DHIS2 demo snapshot — the live server is unreachable)._\n\n" + answer
        for chunk in _chunk(answer, 64):
            yield sse("token", {"text": chunk})
            await asyncio.sleep(0)

    logger.info(
        "agent_done",
        extra={
            "session_id": state["session_id"],
            "mode": mode,
            "duration_ms": int((time.monotonic() - pipeline_start) * 1000),
        },
    )
    yield _timings_event(pipeline_start, _nodes, _llm_calls)
    yield sse("done")


def _map_summary(payload: dict) -> str:
    """One deterministic line: highest / lowest area and how many are drawn."""
    from backend.app.agent.map_builder import fmt_value

    rows = [r for r in (payload.get("table") or {}).get("rows") or [] if r[1] is not None]
    if not rows:
        return ""
    unit = payload.get("unit") or ""
    level = (payload.get("level_name") or "area").lower()
    hi, lo = rows[0], rows[-1]
    drawn = len((payload.get("geojson") or {}).get("features") or []) + len(payload.get("points") or [])
    line = f"**Highest:** {hi[0]} ({fmt_value(float(hi[1]), unit)})"
    if len(rows) > 1:
        line += f" · **Lowest:** {lo[0]} ({fmt_value(float(lo[1]), unit)})"
    line += f" · {drawn} {_plural_word(level, drawn)} on the map"
    notes = payload.get("notes") or []
    if notes:
        line += "\n\n" + "\n".join(f"_{n}_" for n in notes)
    return line


def _plural_word(word: str, n: int) -> str:
    if n == 1:
        return word
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy")):
        return word[:-1] + "ies"
    return word + "s"


def _data_source_event(tracker: dict, settings: Settings) -> dict | None:
    """Which DHIS2 source served this answer (for the per-answer badge)."""
    from backend.app.dhis2 import failover

    mode = tracker.get("mode")
    if not mode:
        return None
    event = {"mode": mode, "base_url": tracker.get("base_url")}
    if mode == "cached":
        event["label"] = failover.snapshot_label(settings)
        event["recorded_at"] = failover.get_store(settings).recorded_at
    elif mode == "fallback":
        event["label"] = f"Fallback DHIS2 instance: {tracker.get('base_url')}"
    else:
        event["label"] = "Live DHIS2"
    return event


def _chunk(text: str, size: int) -> list[str]:
    return [text[i: i + size] for i in range(0, len(text), size)]


def _user_friendly_error(error_msg: str) -> str:
    """Extract a user-friendly message from a DHIS2 error."""
    if "409" in error_msg and "aggregation type" in error_msg.lower():
        return "This specific data element cannot be aggregated over time or locations. Please try a different metric that represents a count or number."
    if "409" in error_msg and "referenced table does not exist" in error_msg.lower():
        return "The analytics tables for this data have not been generated in DHIS2. Please ensure the DHIS2 analytics export job has been run."
    if "409" in error_msg and "dimension is present" in error_msg.lower():
        return "The health indicator could not be correctly identified in the DHIS2 instance. Try rephrasing with more specific terminology (e.g. 'Malaria confirmed cases' instead of 'malaria')."
    if "409" in error_msg and "period" in error_msg.lower():
        return "The requested time period was not recognized by DHIS2."
    if "not in the offline demo snapshot" in error_msg:
        return error_msg
    if "No indicators could be resolved" in error_msg:
        return error_msg  # Already user-friendly from our pre-flight check
    if "metadata resolution succeeds" in error_msg:
        return "The health indicator or location couldn't be definitively matched in the DHIS2 database. Try using more specific names."
    if "404" in error_msg:
        return "The requested resource was not found on the DHIS2 server."
    if "401" in error_msg or "403" in error_msg:
        return "Authentication failed — check DHIS2 credentials."
    if "500" in error_msg:
        return "The DHIS2 server encountered an internal error."
    if "ConnectError" in error_msg or "timeout" in error_msg.lower():
        return "Could not connect to the DHIS2 server."
    return "An unexpected error occurred while fetching data."


def _no_data_html(state: AgentState) -> str:
    """Structured HTML response when DHIS2 returned zero rows."""
    metrics = state.get("metrics", [])
    metric_label = metrics[0]["label"] if metrics else "the requested indicator"
    org_label = state.get("org_unit", {}).get("label", "the selected area")
    periods = ", ".join(state.get("periods", []))

    if state.get("fetch_error"):
        return (
            f"<h2>Data Retrieval Error</h2>"
            f"<p>The DHIS2 server could not process the request for "
            f"<em>{metric_label}</em> in <strong>{org_label}</strong> "
            f"for the period(s): <code>{periods}</code>.</p>"
            f"<div style='margin-top: 1rem; padding: 1rem; background-color: #fef2f2; color: #991b1b; border-left: 4px solid #dc2626; border-radius: 4px;'>"
            f"<strong>Error Detail:</strong> {state['fetch_error']}"
            f"</div>"
            f"<p style='margin-top: 1rem;'>Please verify your request parameters and try again.</p>"
        )

    return (
        f"<h2>No Data Available</h2>"
        f"<p>The DHIS2 server returned <strong>no data</strong> for "
        f"<em>{metric_label}</em> in <strong>{org_label}</strong> "
        f"for the period(s): <code>{periods}</code>.</p>"
        f"<h3>Possible causes</h3>"
        f"<ul>"
        f"<li>The indicator name may not match any data element in this DHIS2 instance.</li>"
        f"<li>The organisation unit may not exist or may have a different name.</li>"
        f"<li>No data has been reported for the selected period.</li>"
        f"<li>The DHIS2 instance may be temporarily unavailable.</li>"
        f"</ul>"
        f"<p>Please verify the indicator, location, and time period, then try again.</p>"
    )


_TYPE_LABEL = {
    "indicator": "indicator",
    "dataElement": "data element",
    "programIndicator": "program indicator",
    "reportingRate": "reporting rate",
}


async def _try_fallback_candidates(state: AgentState, settings: Settings, limit: int = 3) -> AgentState:
    """0 rows for the chosen item: try up to ``limit`` alternates for the SAME concept.

    Alternates come from the resolver (judged to measure the same thing). The
    substitution is recorded in ``resolution_notes`` so the answer says it.
    """
    metrics = state.get("metrics") or []
    if len(metrics) != 1:
        return state
    original = metrics[0]
    tried = 0
    for alt in (original.get("fallbacks") or [])[:limit]:
        tried += 1
        trial = {**original, **alt, "uid_confidence": 0.9, "fallbacks": []}
        state["metrics"] = [trial]
        try:
            state = await _timed_node("fetch_dhis2_fallback", fetch_dhis2, state, settings)
        except Exception as exc:
            logger.info("fallback_candidate_failed", extra={"uid": alt.get("uid"), "error": str(exc)[:200]})
            continue
        if (state.get("dhis2_data") or {}).get("rows"):
            state.setdefault("resolution_notes", []).append(
                f"“{original.get('label')}” has no data for this place and period, so I used the next-best "
                f"match for the same measure: “{alt.get('label')}”."
            )
            logger.info("fallback_candidate_used", extra={"from": original.get("uid"), "to": alt.get("uid")})
            return state
    state["metrics"] = [original]
    if tried:
        state.setdefault("resolution_notes", []).append(
            f"No data for “{original.get('label')}” or {tried} similar item(s) for this place and period."
        )
    return state


def _data_used_text(state: AgentState) -> str:
    """One short line an expert can check: data item(s) + UID, org unit + level, period."""
    metrics = [m for m in state.get("metrics") or [] if m.get("uid") and m.get("uid") != "UNRESOLVED"]
    if not metrics:
        return ""
    items = "; ".join(
        f"{m.get('label')} ({_TYPE_LABEL.get(m.get('object_type'), m.get('object_type') or 'item')} `{m.get('uid')}`)"
        for m in metrics
    )
    org = state.get("org_unit") or {}
    label = org.get("label") or "National"
    uid = org.get("uid") or ""
    org_txt = label
    if uid and re.match(r"^[A-Za-z][A-Za-z0-9]{10}$", uid):
        org_txt += f" (`{uid}`)"
    elif label.lower() in {"national", "federal"}:
        org_txt = "National (your top org unit)"
    else:
        org_txt = f"your top org unit (USER_ORGUNIT), because “{label}” was not recognised as a place in this instance"
    lvl_name = org.get("level_name")
    if lvl_name and uid and re.match(r"^[A-Za-z][A-Za-z0-9]{10}$", uid):
        org_txt += f", {lvl_name.lower()} level"
    if org.get("breakdown_level"):
        org_txt += f", broken down by {org.get('breakdown_level_name') or 'level'} (LEVEL-{org['breakdown_level']})"
    elif "ou_children" in (state.get("disaggregations") or []):
        org_txt += ", broken down one level down"
    cats = state.get("category_labels") or {}
    cat_txt = ""
    used_cats = [cats.get(d, d) for d in state.get("disaggregations") or [] if d != "ou_children"]
    if used_cats:
        cat_txt = " · **Disaggregated by:** " + ", ".join(used_cats)
    pe_list = list(state.get("periods") or [])
    periods = (", ".join(pe_list) if len(pe_list) <= 4
               else f"{pe_list[0]} … {pe_list[-1]} ({len(pe_list)} periods)")
    pe_names = _period_names(state)
    if pe_names:
        periods += f" → {pe_names}"
    line = f"**Data used:** {items} · **Org unit:** {org_txt} · **Period:** {periods}{cat_txt}"
    formulas = [m for m in metrics if m.get("formula")]
    for m in formulas:
        f = m["formula"]
        line += (f"\n\n**{m.get('label')} formula:** numerator = {f.get('numerator') or '?'}; "
                 f"denominator = {f.get('denominator') or '?'}"
                 + (f" ({f.get('type')}, factor {f.get('factor')})" if f.get("type") else ""))
    numden = ((state.get("dhis2_data") or {}).get("metadata") or {}).get("numden") or []
    if numden:
        shown = numden[:15]
        rows = "\n".join(
            f"- {r['org']} · {r['period']}: value {r['value']} = numerator {_fmt_num(r['numerator'])} / "
            f"denominator {_fmt_num(r['denominator'])}" for r in shown
        )
        more = f"\n- … {len(numden) - len(shown)} more rows" if len(numden) > len(shown) else ""
        line += "\n\n**Numerator and denominator (from DHIS2 analytics):**\n" + rows + more
    notes = state.get("resolution_notes") or []
    if notes:
        line += "\n\n" + "\n".join(f"_{n}_" for n in notes)
    return line


def _period_names(state: AgentState) -> str:
    meta = ((state.get("dhis2_data") or {}).get("metadata") or {}).get("raw_metadata") or {}
    items = meta.get("items") or {}
    # Periods present in the returned rows, in order.
    seen: list[str] = []
    for row in (state.get("dhis2_data") or {}).get("rows") or []:
        if len(row) > 1 and row[1] not in seen:
            seen.append(row[1])
    if not seen:
        return ""
    dim_pe = (meta.get("dimensions") or {}).get("pe") or []
    if dim_pe:
        order = []
        for p in dim_pe:
            name = items.get(p, {}).get("name", p) if isinstance(items.get(p), dict) else p
            for cand in (p, name):
                if cand in seen and cand not in order:
                    order.append(cand)
        order += [p for p in seen if p not in order]
    else:
        order = seen
    names = [items.get(p, {}).get("name", p) if isinstance(items.get(p), dict) else p for p in order]
    if len(names) <= 3:
        return ", ".join(str(n) for n in names)
    return f"{names[0]} … {names[-1]} ({len(names)} periods with data)"


def _fmt_num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.0f}" if abs(f - round(f)) < 1e-9 else f"{f:,.2f}"
