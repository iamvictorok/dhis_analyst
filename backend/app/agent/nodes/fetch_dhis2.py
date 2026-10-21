"""DHIS2 data retrieval node.

Handles three strategies:
- analytics_api (default): DHIS2 Analytics API via DHIS2Client
- direct_sql (gated by ENABLE_DIRECT_SQL=true): validated read-only SQL
- both: merge results from analytics_api and direct_sql

Falls back to deterministic mock data when no DHIS2 credentials are configured.
"""
from __future__ import annotations

import logging

from backend.config import Settings
from backend.app.agent.state import AgentState
from backend.app.dhis2.analytics import build_analytics_params, normalise_analytics_response
from backend.app.dhis2.client import DHIS2Client

logger = logging.getLogger("dhis2_analyst.fetch_dhis2")


_ANALYTICS_TYPES = {"indicator", "dataElement", "programIndicator", "reportingRate"}


def effective_strategy(state: AgentState) -> str:
    """The retrieval path actually used for this request.

    The intent parser picks ``tracker_api`` / ``data_value_sets`` whenever the
    user mentions a tracker, programme or raw entries, but those paths return
    unaggregated event lists, need a single resolved org unit, and cannot
    answer "how many" questions. When every requested item resolved to
    something the analytics API can aggregate (data elements, indicators,
    program indicators, reporting rates), answer through analytics instead:
    program indicators are aggregated there per org unit and period exactly
    like routine data. The raw paths are kept only for requests with no
    aggregatable item and a resolved org unit; anything else uses analytics,
    so a tracker question never fails on the "National" org unit.
    """
    from backend.app.dhis2.analytics import DHIS2_UID_RE, REPORTING_RATE_RE

    strategy = state.get("data_retrieval_strategy") or "analytics_api"
    if strategy not in {"tracker_api", "data_value_sets"}:
        return strategy
    metrics = state.get("metrics") or []

    def aggregatable(m: dict) -> bool:
        uid = str(m.get("uid") or "")
        return (m.get("object_type") in _ANALYTICS_TYPES
                and bool(DHIS2_UID_RE.match(uid) or REPORTING_RATE_RE.match(uid)))

    if metrics and all(aggregatable(m) for m in metrics):
        return "analytics_api"
    org_uid = str((state.get("org_unit") or {}).get("uid") or "")
    if not DHIS2_UID_RE.match(org_uid):
        return "analytics_api"
    return strategy


async def fetch_dhis2(state: AgentState, settings: Settings | None = None) -> AgentState:
    requested = state.get("data_retrieval_strategy", "analytics_api")
    strategy = effective_strategy(state)
    if strategy != requested:
        logger.info("dhis2_strategy_rerouted", extra={"requested": requested, "used": strategy})

    has_credentials = bool(
        settings and (
            settings.dhis2_service_account_user
            or state.get("_dhis2_token")
        )
    )

    logger.info(
        "dhis2_fetch_start",
        extra={
            "strategy": strategy,
            "has_credentials": has_credentials,
            "session_id": state.get("session_id"),
            "org_unit": state.get("org_unit"),
            "periods": state.get("periods"),
        }
    )

    if not has_credentials:
        logger.info("dhis2_mock_mode — no credentials configured")
        state["dhis2_data"] = _mock_data(state)
        return state

    client = DHIS2Client(settings, token=state.get("_dhis2_token"))

    if strategy == "analytics_api":
        state["dhis2_data"] = await _fetch_analytics(state, client)
    elif strategy == "tracker_api":
        state["dhis2_data"], state["pii_map"] = await _fetch_tracker(state, client)
    elif strategy == "data_value_sets":
        state["dhis2_data"] = await _fetch_data_value_sets(state, client)
    elif strategy == "direct_sql" and settings and settings.enable_direct_sql:
        state["dhis2_data"] = await _fetch_sql(state, settings)
    elif strategy == "both" and settings and settings.enable_direct_sql:
        api_data = await _fetch_analytics(state, client)
        sql_data = await _fetch_sql(state, settings)
        state["dhis2_data"] = _merge(api_data, sql_data)
    else:
        state["dhis2_data"] = await _fetch_analytics(state, client)

    logger.info(
        "dhis2_fetch_ok",
        extra={
            "strategy": strategy,
            "row_count": len(state["dhis2_data"].get("rows", [])),
            "session_id": state["session_id"],
        },
    )
    return state


async def _fetch_analytics(state: AgentState, client: DHIS2Client) -> dict:
    params = build_analytics_params(state)
    raw = await client.analytics(params)
    return normalise_analytics_response(raw, params)


async def _fetch_tracker(state: AgentState, client: DHIS2Client) -> tuple[dict, dict[str, str]]:
    """Fetch tracker events and anonymize PII before it reaches the LLM."""
    import uuid

    org_uid = state["org_unit"].get("uid", "")
    if not org_uid or org_uid == "UNRESOLVED":
        raise ValueError("Cannot fetch tracker events before organisation unit resolution succeeds.")
    
    params = {
        "orgUnit": org_uid,
        "pageSize": 50,
    }
    
    try:
        raw = await client.tracker(params)
    except Exception:
        logger.warning(
            "tracker_api_failed",
            extra={"session_id": state.get("session_id"), "org_unit": params["orgUnit"]},
            exc_info=True,
        )
        raise
        
    pii_map = {}
    rows = []
    
    # Extract instances (DHIS2 Tracker response structure varies by version)
    instances = raw.get("instances", [])

    if not instances:
        logger.info(
            "tracker_api_empty",
            extra={"session_id": state.get("session_id"), "org_unit": params["orgUnit"]},
        )

    for event in instances:
        token = f"Patient_{str(uuid.uuid4())[:6]}"
        pii_map[token] = event.get("trackedEntity", event.get("trackedEntityInstance", "unknown"))
        rows.append([
            token,
            event.get("occurredAt") or event.get("eventDate") or event.get("createdAt"),
            event.get("status", "UNKNOWN"),
        ])
            
    return {
        "rows": rows,
        "headers": ["Patient ID", "Event Date", "Status"],
        "metadata": {
            "data_source": "tracker_api_anonymized",
            "indicators": state["metrics"],
            "org_units": [state["org_unit"]],
        }
    }, pii_map


async def _fetch_data_value_sets(state: AgentState, client: DHIS2Client) -> dict:
    """Fetch raw data entry values before aggregation."""
    org_uid = state["org_unit"].get("uid", "")
    data_element_uid = state["metrics"][0]["uid"] if state["metrics"] else ""
    if not org_uid or org_uid == "UNRESOLVED" or not data_element_uid or data_element_uid == "UNRESOLVED":
        raise ValueError("Cannot fetch raw data values before metadata resolution succeeds.")

    params = {
        "orgUnit": org_uid,
        "dataElement": data_element_uid,
        "period": state["periods"][0] if state["periods"] else "THIS_YEAR"
    }
    
    try:
        raw = await client.data_value_sets(params)
    except Exception:
        logger.warning(
            "data_value_sets_api_failed",
            extra={
                "session_id": state.get("session_id"),
                "org_unit": params["orgUnit"],
                "data_element": params["dataElement"],
                "period": params["period"],
            },
            exc_info=True,
        )
        raise
        
    data_values = raw.get("dataValues", [])
    rows = []
    
    for value in data_values:
        rows.append([
            value.get("period"),
            value.get("value"),
            value.get("storedBy") or value.get("lastUpdated") or "reported",
        ])
            
    return {
        "rows": rows,
        "headers": ["Entry Period", "Raw Value", "Data Quality Status"],
        "metadata": {
            "data_source": "data_value_sets_api",
            "indicators": state["metrics"],
            "org_units": [state["org_unit"]],
        }
    }


async def _fetch_sql(state: AgentState, settings: Settings) -> dict:
    from backend.app.agent.nodes.fetch_sql import build_analytics_sql, execute_validated_sql
    from backend.app.db.session import get_db_session

    sql = build_analytics_sql(state)
    async with get_db_session() as session:
        rows, headers = await execute_validated_sql(sql, session, settings.sql_timeout_seconds)

    return {
        "rows": rows,
        "headers": headers,
        "metadata": {
            "data_source": "direct_sql",
            "indicators": state["metrics"],
            "org_units": [state["org_unit"]],
            "periods": state["periods"],
        },
    }


def _merge(api_data: dict, sql_data: dict) -> dict:
    """Merge rows from both sources, preferring API data. Deduplicates by first 3 columns."""
    api_rows = api_data.get("rows", [])
    sql_rows = sql_data.get("rows", [])
    seen = {tuple(r[:3]) for r in api_rows}
    extra = [r for r in sql_rows if tuple(r[:3]) not in seen]
    return {
        **api_data,
        "rows": api_rows + extra,
        "metadata": {**api_data.get("metadata", {}), "data_source": "both", "sql_extra_rows": len(extra)},
    }


def _mock_data(state: AgentState) -> dict:
    """Deterministic mock — used when no DHIS2 credentials are present."""
    metric = state["metrics"][0]["label"] if state["metrics"] else "Indicator"
    org = state["org_unit"]["label"]
    rows = []
    
    # Generate multiple locations if ou_children was requested
    orgs_to_mock = [org]
    if "ou_children" in state.get("disaggregations", []):
        orgs_to_mock = [f"{org} North", f"{org} South", f"{org} East", f"{org} West"]
        
    base_value = 1200
    for o_idx, current_org in enumerate(orgs_to_mock):
        for p_idx, period in enumerate(state["periods"]):
            # Simulate slight trend variation and location variation
            value = base_value + (p_idx * 113) - (p_idx * p_idx * 7) + (o_idx * 250)
            rows.append([current_org, period, max(0, value), metric])

    return {
        "rows": rows,
        "headers": ["Organisation unit", "Period", "Value", "Metric"],
        "metadata": {
            "indicators": state["metrics"],
            "org_units": [state["org_unit"]],
            "periods": state["periods"],
            "data_source": "mock_analytics_api",
        },
    }
