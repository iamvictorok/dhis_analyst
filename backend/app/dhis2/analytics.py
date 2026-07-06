from __future__ import annotations

import logging
import re
from typing import Any

from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.analytics")

DHIS2_UID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{10}$")


def _join_dimension_items(items: list[str]) -> str:
    """DHIS2 analytics uses semicolon-delimited items within one dimension."""
    return ";".join(item for item in items if item)


def build_analytics_params(state: AgentState) -> dict[str, Any]:
    logger.info(
        "build_analytics_params_start",
        extra={
            "metrics_count": len(state.get("metrics", [])),
            "org_unit": state.get("org_unit"),
            "periods": state.get("periods"),
        }
    )
    dx_items = [m["uid"] for m in state["metrics"] if m.get("uid")]
    dx = _join_dimension_items(dx_items)
    
    ou_level = state["org_unit"]["level"]
    ou_uid = state["org_unit"].get("uid", "")
    ou_label = state["org_unit"].get("label", "")
    disaggregations = state.get("disaggregations", [])
    if "ou_children" in disaggregations:
        ou_level += 1
        disaggregations = [d for d in disaggregations if d != "ou_children"]

    if "ou_children" in state.get("disaggregations", []) and DHIS2_UID_RE.match(ou_uid):
        ou = f"LEVEL-{ou_level};{ou_uid}"
    elif DHIS2_UID_RE.match(ou_uid):
        ou = ou_uid
    elif ou_label.lower() in {"national", "federal"}:
        ou = "USER_ORGUNIT"
    else:
        logger.warning(
            "analytics_org_unit_unresolved",
            extra={"org_unit_label": ou_label, "org_unit_uid": ou_uid},
        )
        ou = ou_uid or "USER_ORGUNIT"

    pe = _join_dimension_items(state["periods"])
    
    params: dict[str, Any] = {
        "dimension": [f"dx:{dx}", f"ou:{ou}", f"pe:{pe}"],
        "displayProperty": "NAME",
        "outputIdScheme": "NAME",
        "skipMeta": "false",
    }
    if disaggregations:
        params["dimension"].append(f"co:{_join_dimension_items(disaggregations)}")
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
    width = len(headers)
    malformed_rows = sum(1 for row in rows if width and len(row) != width)
    if malformed_rows:
        logger.warning(
            "analytics_response_row_width_mismatch",
            extra={
                "rows_count": len(rows),
                "headers_count": width,
                "malformed_rows": malformed_rows,
            },
        )
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
