from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.analytics")


def build_analytics_params(state: AgentState) -> dict[str, Any]:
    logger.info(
        "build_analytics_params_start",
        extra={
            "metrics_count": len(state.get("metrics", [])),
            "org_unit": state.get("org_unit"),
            "periods": state.get("periods"),
        }
    )
    dx = ",".join(m["uid"] for m in state["metrics"])
    
    ou_level = state["org_unit"]["level"]
    disaggregations = state.get("disaggregations", [])
    if "ou_children" in disaggregations:
        ou_level += 1
        disaggregations = [d for d in disaggregations if d != "ou_children"]
        
    ou = f"LEVEL-{ou_level};{state['org_unit']['uid']}"
    pe = ",".join(state["periods"])
    
    params: dict[str, Any] = {
        "dimension": [f"dx:{dx}", f"ou:{ou}", f"pe:{pe}"],
        "displayProperty": "NAME",
        "outputIdScheme": "NAME",
        "skipMeta": "false",
    }
    if disaggregations:
        params["dimension"].append(f"co:{','.join(disaggregations)}")
    logger.info("build_analytics_params_complete", extra={"params": params})
    return params


def normalise_analytics_response(raw: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    logger.info(
        "normalise_analytics_response_start",
        extra={
            "rows_count": len(raw.get("rows", [])),
            "headers_count": len(raw.get("headers", [])),
        }
    )
    headers = [h.get("column", h.get("name", "")) for h in raw.get("headers", [])]
    rows = raw.get("rows", [])
    metadata = raw.get("metaData", {})
    normalised = {
        "rows": rows,
        "headers": headers or ["Organisation unit", "Period", "Value"],
        "metadata": {
            "raw_metadata": metadata,
            "data_source": "analytics_api",
            "analytics_params": params,
        },
    }
    logger.info("normalise_analytics_response_complete", extra={"rows_count": len(rows)})
    return normalised
