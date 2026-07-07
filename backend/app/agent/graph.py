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
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable

from backend.config import Settings
from backend.app.agent.intent import classify_intent_llm
from backend.app.agent.nodes.enrich_web import enrich_web
from backend.app.agent.nodes.evidence_fusion import fuse_evidence
from backend.app.agent.nodes.fetch_dhis2 import fetch_dhis2
from backend.app.agent.nodes.generate_content import generate_content
from backend.app.agent.nodes.metadata_resolve import resolve_metadata

from backend.app.agent.renderers.export import render_export_payload
from backend.app.agent.state import AgentState
from backend.app.models import ChatRequest, Identity

logger = logging.getLogger("dhis2_analyst.graph")


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

    try:
        state = await build_initial_state(request, settings, identity, history, dhis2_token)
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
        yield sse("done")
        return

    # --- Clarification gate ---
    if state["clarification_needed"]:
        yield sse("clarification", {"question": state["clarification_question"]})
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
            yield sse("done")
            return

        if state["clarification_needed"]:
            yield sse("clarification", {"question": state["clarification_question"]})
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
                })
                # Non-fatal — continue with empty data

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
    else:
        logger.info("skipping_data_pipeline", extra={"reason": "requires_data=False", "session_id": state.get("session_id")})

    # --- Empty data guard: refuse to hallucinate from nothing ---
    if state["requires_data"] and not state.get("dhis2_data", {}).get("rows"):
        mode = state["output_mode"]
        if mode in ("report", "dashboard", "presentation", "export", "map"):
            yield sse("report_html", {"html": _no_data_html(state)})
            logger.info("agent_done_no_data", extra={"session_id": state["session_id"], "mode": mode})
            yield sse("done")
            return

    # --- Content generation + routing ---
    try:
        state = await _timed_node("generate_content", generate_content, state, settings)
    except Exception as exc:
        logger.error("content_generation_failed", extra={"error": str(exc)})
        yield sse("error", {"code": "CONTENT_FAILED", "user_message": "Content generation failed.", "detail": str(exc)})
        yield sse("done")
        return

    mode = state["output_mode"]

    if mode == "dashboard":
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
        yield sse("report_html", {"html": state["active_report_html"]})
    elif mode == "presentation":
        yield sse("slide_manifest", state["active_slide_manifest"])
    elif mode == "export":
        payload = render_export_payload(state)
        yield sse("data_ready", {
            "file_id": None,
            "format": "json",
            "row_count": len(payload.get("rows", [])),
            "data": payload,
        })
    else:
        # Conversational — stream token by token
        answer = state.get("active_conversational_response") or ""
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
    yield sse("done")


def _chunk(text: str, size: int) -> list[str]:
    return [text[i: i + size] for i in range(0, len(text), size)]


def _user_friendly_error(error_msg: str) -> str:
    """Extract a user-friendly message from a DHIS2 error."""
    if "409" in error_msg and "aggregation type" in error_msg.lower():
        return "This specific data element cannot be aggregated over time or locations. Please try a different metric that represents a count or number."
    if "409" in error_msg and "referenced table does not exist" in error_msg.lower():
        return "The analytics tables for this data have not been generated in DHIS2. Please ensure the DHIS2 analytics export job has been run."
    if "409" in error_msg and "dimension is present" in error_msg.lower():
        return "The query was constructed with a dimension that has no valid options. This usually means the health indicator could not be correctly identified in the DHIS2 instance."
    if "409" in error_msg and "period" in error_msg.lower():
        return "The requested time period was not recognized by DHIS2."
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
