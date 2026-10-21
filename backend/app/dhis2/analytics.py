from __future__ import annotations

import logging
import re
from typing import Any

from backend.app.agent.state import AgentState

logger = logging.getLogger("dhis2_analyst.analytics")

DHIS2_UID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{10}$")
# Data set reporting metrics, e.g. "BfMAe6Itzgt.REPORTING_RATE".
REPORTING_RATE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]{10}\.(REPORTING_RATE|REPORTING_RATE_ON_TIME|ACTUAL_REPORTS|ACTUAL_REPORTS_ON_TIME|EXPECTED_REPORTS)$"
)


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
    # Filter out UNRESOLVED and empty UIDs — sending them to DHIS2 causes
    # cryptic 409 errors ("dimension is present without valid options").
    # Also validate object_type: only indicator-compatible types belong in dx.
    _DX_VALID_TYPES = {"indicator", "dataElement", "programIndicator"}
    dx_items = []
    for m in state["metrics"]:
        uid = m.get("uid", "")
        obj_type = m.get("object_type", "indicator")
        if obj_type == "reportingRate" and REPORTING_RATE_RE.match(uid or ""):
            dx_items.append(uid)
            continue
        if not uid or uid == "UNRESOLVED" or not DHIS2_UID_RE.match(uid):
            continue
        if obj_type not in _DX_VALID_TYPES:
            logger.warning(
                "analytics_dx_type_mismatch",
                extra={"uid": uid, "label": m.get("label"), "object_type": obj_type},
            )
            continue
        dx_items.append(uid)
    if not dx_items:
        resolved_labels = [m.get("label", "?") for m in state["metrics"]]
        raise ValueError(
            f"No indicators could be resolved to valid DHIS2 UIDs. "
            f"The search terms ({', '.join(resolved_labels)}) did not match any "
            f"indicator in this DHIS2 instance. Please try different wording."
        )
    dx = _join_dimension_items(dx_items)
    
    ou_level = state["org_unit"]["level"]
    ou_uid = state["org_unit"].get("uid", "")
    ou_label = state["org_unit"].get("label", "")
    all_disaggregations = state.get("disaggregations", [])
    wants_children = "ou_children" in all_disaggregations
    disaggregations = [d for d in all_disaggregations if d != "ou_children"]
    child_level = ou_level + 1 if wants_children else ou_level

    ou_is_uid = bool(DHIS2_UID_RE.match(ou_uid))
    is_national = ou_label.lower() in {"national", "federal"}

    if ou_is_uid and wants_children:
        ou = f"LEVEL-{child_level};{ou_uid}"
    elif ou_is_uid:
        ou = ou_uid
    elif is_national:
        # Honour a per-child breakdown for national scope too, instead of
        # silently dropping it and returning a single aggregate row.
        ou = f"USER_ORGUNIT;LEVEL-{child_level}" if wants_children else "USER_ORGUNIT"
    else:
        # A named org unit that never resolved to a real UID (e.g. "UNRESOLVED"
        # or a leftover label) must NOT be sent as a dimension item — DHIS2
        # rejects it with a 409. Fall back to the caller's own org scope.
        logger.warning(
            "analytics_org_unit_unresolved",
            extra={"org_unit_label": ou_label, "org_unit_uid": ou_uid},
        )
        ou = f"USER_ORGUNIT;LEVEL-{child_level}" if wants_children else "USER_ORGUNIT"

    pe = _join_dimension_items(state["periods"])

    params: dict[str, Any] = {
        "dimension": [f"dx:{dx}", f"ou:{ou}", f"pe:{pe}"],
        "displayProperty": "NAME",
        "outputIdScheme": "NAME",
        "skipMeta": "false",
    }
    # Append valid category UIDs as dimensions. Free-text
    # tokens that failed to resolve in metadata_resolve are dropped to avoid 409s.
    category_uids = [d for d in disaggregations if DHIS2_UID_RE.match(d)]
    dropped = [d for d in disaggregations if not DHIS2_UID_RE.match(d)]
    if dropped:
        logger.warning("analytics_dropped_invalid_disaggregations", extra={"tokens": dropped})
    
    # Each category UID must be appended as its own dimension (e.g. dimension=sex12345678)
    for cat_uid in category_uids:
        params["dimension"].append(cat_uid)
        
    logger.info("build_analytics_params_complete", extra={"params": params})
    return params


# Canonical internal row layout shared with the mock data path and every
# renderer/node that consumes analytics rows positionally.
CANONICAL_HEADERS = ["Organisation unit", "Period", "Value", "Metric"]


def normalise_analytics_response(raw: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Reorder a raw DHIS2 analytics response into the canonical row layout.

    DHIS2 returns columns in requested-dimension order (dx, ou, pe, ...) with
    the numeric value last — e.g. ``[metric, org, period, value]``. Every
    downstream consumer, however, assumes the mock layout
    ``[org, period, value, metric]``. Without reordering here, real data renders
    as zeros/garbage while mock mode looks perfect — a primary cause of the
    "works sometimes" behaviour. We map columns by their DHIS2 header id
    (``dx``/``ou``/``pe``/``value``) and fall back to pass-through only when the
    layout is unrecognised.
    """
    raw_headers = raw.get("headers", [])
    rows = raw.get("rows", [])
    metadata = raw.get("metaData", {})

    logger.info(
        "normalise_analytics_response_start",
        extra={"rows_count": len(rows), "headers_count": len(raw_headers)},
    )

    # DHIS2 headers carry the dimension id in "name"; the test/legacy shape puts
    # it in "column". Check both.
    def header_id(h: dict) -> str:
        return str(h.get("name") or h.get("column") or "").lower()

    col: dict[str, int] = {}
    for i, h in enumerate(raw_headers):
        hid = header_id(h)
        if hid in {"dx", "ou", "pe", "value", "co"} and hid not in col:
            col[hid] = i

    # Reorder only when we can identify the value column plus at least one of the
    # dimension columns; otherwise preserve the original rows untouched.
    if "value" in col and ("ou" in col or "dx" in col or "pe" in col):
        oi, pi, vi, mi = col.get("ou"), col.get("pe"), col.get("value"), col.get("dx")

        def cell(row: list, idx: int | None) -> Any:
            return row[idx] if idx is not None and idx < len(row) else ""

        canonical_rows = [
            [cell(r, oi), cell(r, pi), cell(r, vi), cell(r, mi)] for r in rows
        ]
        normalised = {
            "rows": canonical_rows,
            "headers": CANONICAL_HEADERS,
            "metadata": {
                "raw_metadata": metadata,
                "data_source": "analytics_api",
                "analytics_params": params,
            },
        }
        logger.info(
            "normalise_analytics_response_complete",
            extra={"rows_count": len(canonical_rows), "reordered": True},
        )
        return normalised

    # Unrecognised layout — pass through with best-effort headers.
    headers = [h.get("column", h.get("name", "")) for h in raw_headers]
    logger.warning(
        "analytics_response_layout_unrecognised",
        extra={"headers": headers},
    )
    return {
        "rows": rows,
        "headers": headers or ["Organisation unit", "Period", "Value"],
        "metadata": {
            "raw_metadata": metadata,
            "data_source": "analytics_api",
            "analytics_params": params,
        },
    }
