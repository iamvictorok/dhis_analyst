"""Map (choropleth) builder.

Turns a resolved question into a map payload the frontend draws with
Plotly's MapLibre-based ``choroplethmap`` / ``scattermap`` traces:

1. One analytics call with the item as ``dx``, the org-unit breakdown as ``ou``
   and the period(s) as a *filter*, so each unit gets ONE value for the whole
   period (DHIS2 aggregates numerator and denominator itself; this is the same
   number DHIS2 Maps shows).
2. DHIS2 geometry for the same ``ou`` dimension via ``/api/geoFeatures``,
   cached per instance + ``ou`` dimension (i.e. per parent and level).
3. Join by org-unit UID. Polygons become choropleth areas, points (facilities)
   become circle markers. Units with a value but no geometry are listed in a
   note; units with geometry but no value are drawn grey ("No data").

If nothing can be drawn, :class:`MapUnavailable` carries an honest, user-facing
reason and the caller falls back to a chart.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any

from backend.config import Settings
from backend.app.agent.state import AgentState
from backend.app.dhis2.analytics import DHIS2_UID_RE, REPORTING_RATE_RE, ou_dimension

logger = logging.getLogger("dhis2_analyst.map_builder")

# Sequential palette (light -> dark). Neutral: does not imply good or bad.
PALETTE = ["#fef0d9", "#fdcc8a", "#fc8d59", "#e34a33", "#b30000"]
NO_DATA_COLOR = "#d1d5db"
GEOMETRY_TTL_SECONDS = 6 * 3600
_GEOMETRY_CACHE: dict[tuple[str, str], tuple[float, dict[str, dict]]] = {}
_UNIT_CACHE: dict[tuple[str, str], str] = {}
# Simplification tolerance in degrees (~200 m): keeps payloads small for the
# browser without visible change at district / chiefdom zoom.
SIMPLIFY_TOLERANCE = 0.002


class MapUnavailable(Exception):
    """A map cannot be drawn; ``str(exc)`` is safe to show the user."""


# ---------------------------------------------------------------------------
# Geometry helpers (pure)
# ---------------------------------------------------------------------------

def _rdp(points: list[list[float]], eps: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker on a list of [lon, lat]; iterative, keeps endpoints."""
    n = len(points)
    if n < 3 or eps <= 0:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        ax, ay = points[s][0], points[s][1]
        bx, by = points[e][0], points[e][1]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, -1
        for i in range(s + 1, e):
            px, py = points[i][0], points[i][1]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, idx = d, i
        if best > eps and idx > 0:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, k in zip(points, keep) if k]


def _simplify_ring(ring: list, eps: float) -> list | None:
    pts = [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
    if len(pts) < 4:
        return pts if len(pts) >= 3 else None
    out = _rdp(pts, eps)
    if out[0] != out[-1]:
        out.append(out[0])
    if len(out) < 4:
        # Too small to survive simplification: keep the original ring.
        out = pts if pts[0] == pts[-1] else pts + [pts[0]]
    return out


def simplify_geometry(geom: dict, eps: float = SIMPLIFY_TOLERANCE) -> dict | None:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        return geom
    if gtype == "Polygon":
        rings = [r for r in (_simplify_ring(r, eps) for r in coords or []) if r]
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if gtype == "MultiPolygon":
        polys = []
        for poly in coords or []:
            rings = [r for r in (_simplify_ring(r, eps) for r in poly or []) if r]
            if rings:
                polys.append(rings)
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    return None


def geo_feature_to_geometry(f: dict) -> dict | None:
    """DHIS2 ``/api/geoFeatures`` item -> GeoJSON geometry (ty 1 point, 2 polygon, 3 multipolygon)."""
    co = f.get("co")
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except ValueError:
            return None
    if not co:
        return None
    ty = f.get("ty")
    try:
        if ty == 1:
            return {"type": "Point", "coordinates": [float(co[0]), float(co[1])]}
        if ty == 2:
            # DHIS2 "polygon" may be a list of rings or a MultiPolygon-shaped list.
            depth = _depth(co)
            if depth == 3:
                return {"type": "Polygon", "coordinates": co}
            if depth == 4:
                return {"type": "MultiPolygon", "coordinates": co}
        if ty == 3:
            depth = _depth(co)
            if depth == 4:
                return {"type": "MultiPolygon", "coordinates": co}
            if depth == 3:
                return {"type": "Polygon", "coordinates": co}
    except (TypeError, ValueError, IndexError):
        return None
    return None


def org_unit_geometry_to_geojson(g: dict | None) -> dict | None:
    """``organisationUnits?fields=geometry`` value -> GeoJSON geometry (already GeoJSON)."""
    if isinstance(g, dict) and g.get("type") in {"Point", "Polygon", "MultiPolygon"} and g.get("coordinates"):
        return {"type": g["type"], "coordinates": g["coordinates"]}
    return None


def _depth(x: Any) -> int:
    d = 0
    while isinstance(x, list) and x:
        d += 1
        x = x[0]
    return d


def _bbox(geoms: list[dict]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(c):
        if isinstance(c, list) and c and isinstance(c[0], (int, float)):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
        elif isinstance(c, list):
            for x in c:
                walk(x)

    for g in geoms:
        walk(g.get("coordinates"))
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


# ---------------------------------------------------------------------------
# Legend classes (pure)
# ---------------------------------------------------------------------------

def _nice(v: float) -> float:
    if v == 0:
        return 0.0
    mag = 10 ** math.floor(math.log10(abs(v)))
    step = mag / 10 if abs(v) >= 10 else mag / 100
    return round(round(v / step) * step, 6)


def legend_classes(values: list[float], max_classes: int = 5) -> list[dict]:
    """Equal-count (quantile) classes, rounded, at most ``max_classes``.

    Returns ``[{"from", "to", "color"}]`` ascending. Every value falls in a class.
    """
    vals = sorted(v for v in values if v is not None and not math.isnan(v))
    if not vals:
        return []
    distinct = sorted(set(vals))
    k = min(max_classes, len(distinct))
    if k == 1:
        return [{"from": distinct[0], "to": distinct[0], "color": PALETTE[2]}]
    breaks = [vals[0]]
    for i in range(1, k):
        q = vals[min(len(vals) - 1, int(round(i * len(vals) / k)))]
        b = _nice(q)
        if b <= breaks[-1] or b >= vals[-1]:
            b = q
        if b > breaks[-1] and b < vals[-1]:
            breaks.append(b)
    breaks.append(vals[-1])
    n = len(breaks) - 1
    colors = _palette(n)
    return [{"from": breaks[i], "to": breaks[i + 1], "color": colors[i]} for i in range(n)]


def _palette(n: int) -> list[str]:
    if n >= len(PALETTE):
        return PALETTE[:n]
    idx = [round(i * (len(PALETTE) - 1) / max(1, n - 1)) for i in range(n)]
    return [PALETTE[i] for i in idx]


def class_index(value: float, classes: list[dict]) -> int:
    for i, c in enumerate(classes):
        if value <= c["to"] or i == len(classes) - 1:
            return i
    return len(classes) - 1


def fmt_value(v: float | None, unit: str = "") -> str:
    if v is None:
        return "No data"
    txt = f"{v:,.0f}" if abs(v - round(v)) < 1e-9 and abs(v) >= 100 else (f"{v:,.1f}" if abs(v) >= 10 else f"{v:,.2f}")
    if txt.endswith(".0") or txt.endswith(".00"):
        txt = txt.split(".")[0]
    return f"{txt}{'%' if unit == '%' else ''}"


# ---------------------------------------------------------------------------
# Join (pure)
# ---------------------------------------------------------------------------

def join_values_to_geometry(
    values: dict[str, float],
    names: dict[str, str],
    geometry: dict[str, dict],
    unit: str = "",
) -> dict:
    """Join analytics values (by UID) to geometry (by UID).

    ``geometry`` maps uid -> {"name": str, "geometry": GeoJSON geometry}.
    Returns polygons (FeatureCollection), points, legend, bbox, missing lists.
    """
    classes = legend_classes(list(values.values()))
    features: list[dict] = []
    points: list[dict] = []
    drawn_geoms: list[dict] = []
    no_geometry = sorted((names.get(u, u) for u in values if u not in geometry), key=str.lower)
    for uid, g in geometry.items():
        geom = g.get("geometry")
        if not geom:
            continue
        name = names.get(uid) or g.get("name") or uid
        val = values.get(uid)
        if val is None:
            color, cls = NO_DATA_COLOR, -1
        else:
            cls = class_index(val, classes)
            color = classes[cls]["color"]
        props = {"id": uid, "name": name, "value": val, "label": fmt_value(val, unit), "class": cls, "color": color}
        drawn_geoms.append(geom)
        if geom["type"] == "Point":
            lon, lat = geom["coordinates"][:2]
            points.append({**props, "lon": lon, "lat": lat})
        else:
            features.append({"type": "Feature", "id": uid, "properties": props, "geometry": geom})
    drawn_with_value = sum(1 for f in features if f["properties"]["value"] is not None) + sum(
        1 for p in points if p["value"] is not None)
    legend = [
        {**c, "label": f"{fmt_value(c['from'], unit)} – {fmt_value(c['to'], unit)}"
         if c["from"] != c["to"] else fmt_value(c["from"], unit)}
        for c in classes
    ]
    no_value = sorted(
        [f["properties"]["name"] for f in features if f["properties"]["value"] is None]
        + [p["name"] for p in points if p["value"] is None], key=str.lower)
    return {
        "geojson": {"type": "FeatureCollection", "features": features},
        "points": points,
        "legend": legend,
        "bbox": _bbox(drawn_geoms),
        "drawn_with_value": drawn_with_value,
        "no_geometry": no_geometry,
        "no_value": no_value,
    }


# ---------------------------------------------------------------------------
# DHIS2 I/O
# ---------------------------------------------------------------------------

def _instance_key(settings: Settings, token: str | None) -> str:
    if token:
        return settings.dhis2_base_url.rstrip("/")
    try:
        from backend.app.dhis2 import failover
        mode, base = failover.current_target(settings)
        return base or mode
    except Exception:
        return settings.dhis2_base_url.rstrip("/")


def legacy_coordinates_to_geometry(coordinates: Any, feature_type: str | None) -> dict | None:
    """Pre-2.32 ``coordinates`` (JSON string) + ``featureType`` -> GeoJSON geometry."""
    co = coordinates
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except ValueError:
            return None
    if not co:
        return None
    ft = (feature_type or "").upper()
    ty = {"POINT": 1, "POLYGON": 2, "MULTI_POLYGON": 3, "MULTIPOLYGON": 3}.get(ft)
    if ty is None:
        depth = _depth(co)
        ty = 1 if depth == 1 else (2 if depth == 3 else 3 if depth == 4 else None)
    if ty is None:
        return None
    return geo_feature_to_geometry({"ty": ty, "co": co})


def parse_ou_dimension(ou_dim: str) -> tuple[list[str], int | None, bool]:
    """``LEVEL-3;abc;USER_ORGUNIT`` -> (parent uids, level, uses USER_ORGUNIT)."""
    parents: list[str] = []
    level: int | None = None
    user_ou = False
    for part in (ou_dim or "").split(";"):
        part = part.strip()
        if part.upper().startswith("LEVEL-"):
            try:
                level = int(part.split("-", 1)[1])
            except ValueError:
                pass
        elif part.upper().startswith("USER_ORGUNIT"):
            user_ou = True
        elif DHIS2_UID_RE.match(part):
            parents.append(part)
    return parents, level, user_ou


def _store(out: dict, uid: str, name: str, geom: dict | None, root: str = "") -> None:
    if not uid or not geom:
        return
    geom = simplify_geometry(geom)
    if geom:
        out[uid] = {"name": name or uid, "geometry": geom, "root": root}


async def _geometry_from_geofeatures(client, ou_dim: str) -> dict[str, dict]:
    raw = await client.get("/api/geoFeatures.json", params={"ou": f"ou:{ou_dim}", "displayProperty": "NAME"})
    items = raw if isinstance(raw, list) else (raw or {}).get("geoFeatures") or []
    out: dict[str, dict] = {}
    for f in items:
        root = (str(f.get("pg") or "").split("/") or [""])[0] or str(f.get("pi") or "")
        _store(out, f.get("id"), f.get("na"), geo_feature_to_geometry(f), root)
    return out


async def _geometry_from_org_units(client, ou_dim: str) -> dict[str, dict]:
    """Fallback: ``organisationUnits`` with ``geometry`` (2.32+) or legacy ``coordinates``/``featureType``."""
    parents, level, user_ou = parse_ou_dimension(ou_dim)
    if user_ou:
        me = await client.get("/api/me.json", params={"fields": "organisationUnits[id]"})
        parents += [o["id"] for o in (me or {}).get("organisationUnits") or [] if o.get("id")]
    if not parents:
        return {}
    out: dict[str, dict] = {}
    for parent in dict.fromkeys(parents):
        filters = [f"path:like:{parent}", f"level:eq:{level}"] if level is not None else [f"id:eq:{parent}"]
        params: dict[str, Any] = {
            "fields": "id,displayName,name,path,level,geometry,coordinates,featureType",
            "filter": filters,
            "paging": "false",
        }
        raw = await client.get("/api/organisationUnits.json", params=params)
        for o in (raw or {}).get("organisationUnits") or []:
            geom = org_unit_geometry_to_geojson(o.get("geometry")) or legacy_coordinates_to_geometry(
                o.get("coordinates"), o.get("featureType"))
            path = [p for p in str(o.get("path") or "").split("/") if p]
            _store(out, o.get("id"), o.get("displayName") or o.get("name"), geom, path[0] if path else "")
    return out


async def fetch_geometry(client, settings: Settings, ou_dim: str, token: str | None = None) -> dict[str, dict]:
    """uid -> {"name", "geometry", "root"} for every unit in the ``ou`` dimension (cached).

    Tries ``/api/geoFeatures`` first (every version 2.38-2.42), then the
    ``organisationUnits`` API (``geometry`` or the legacy ``coordinates`` +
    ``featureType``). Uses the same client, so the caller's DHIS2 auth is unchanged.
    """
    key = (_instance_key(settings, token), ou_dim)
    hit = _GEOMETRY_CACHE.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    out: dict[str, dict] = {}
    first_error: Exception | None = None
    try:
        out = await _geometry_from_geofeatures(client, ou_dim)
    except Exception as exc:
        first_error = exc
        logger.info("geofeatures_unavailable", extra={"error": str(exc)[:200]})
    if not out:
        try:
            out = await _geometry_from_org_units(client, ou_dim)
        except Exception as exc:
            logger.info("org_unit_geometry_unavailable", extra={"error": str(exc)[:200]})
            if first_error is not None:
                raise first_error from exc
            raise
    if out:  # never cache a miss: a transient gap must not stick for hours
        _GEOMETRY_CACHE[key] = (now + GEOMETRY_TTL_SECONDS, out)
    return out


async def metric_unit(client, settings: Settings, metric: dict, token: str | None = None) -> str:
    """"%" for percentage indicators / rates, "" for counts."""
    uid = metric.get("uid") or ""
    otype = metric.get("object_type") or "indicator"
    label = (metric.get("label") or "").lower()
    if otype == "reportingRate" or REPORTING_RATE_RE.match(uid):
        return "%" if "REPORTING_RATE" in uid else ""
    if otype == "dataElement":
        return ""
    if otype == "indicator" and DHIS2_UID_RE.match(uid):
        key = (_instance_key(settings, token), uid)
        if key in _UNIT_CACHE:
            return _UNIT_CACHE[key]
        try:
            raw = await client.get(f"/api/indicators/{uid}.json", params={"fields": "indicatorType[factor]"})
            factor = ((raw or {}).get("indicatorType") or {}).get("factor")
            unit = "%" if factor == 100 else ""
            _UNIT_CACHE[key] = unit
            return unit
        except Exception as exc:
            logger.info("indicator_unit_lookup_failed", extra={"error": str(exc)[:200]})
    if re.search(r"%|coverage|\brate\b|percent|proportion", label):
        return "%"
    return ""


def map_analytics_params(state: AgentState, dx: str, ou_dim: str) -> dict[str, Any]:
    periods = ";".join(p for p in state.get("periods") or [] if p) or "THIS_YEAR"
    return {
        "dimension": [f"dx:{dx}", f"ou:{ou_dim}"],
        "filter": [f"pe:{periods}"],
        "displayProperty": "NAME",
        "outputIdScheme": "UID",
        "skipMeta": "false",
    }


def parse_map_analytics(raw: dict) -> tuple[dict[str, float], dict[str, str], list[str]]:
    """-> (uid -> value, uid -> name, period names)."""
    headers = [str(h.get("name") or h.get("column") or "").lower() for h in raw.get("headers") or []]
    try:
        oi, vi = headers.index("ou"), headers.index("value")
    except ValueError:
        return {}, {}, []
    items = ((raw.get("metaData") or {}).get("items")) or {}
    values: dict[str, float] = {}
    for r in raw.get("rows") or []:
        try:
            values[str(r[oi])] = float(r[vi])
        except (TypeError, ValueError, IndexError):
            continue
    names = {u: str((items.get(u) or {}).get("name") or u) for u in values}
    for u, it in items.items():
        if isinstance(it, dict) and DHIS2_UID_RE.match(u) and u not in names:
            names[u] = str(it.get("name") or u)
    pe_ids = ((raw.get("metaData") or {}).get("dimensions") or {}).get("pe") or []
    pe_names = [str((items.get(p) or {}).get("name") or p) for p in pe_ids]
    return values, names, pe_names


def _period_label(state: AgentState, pe_names: list[str], items: dict | None = None) -> str:
    items = items or {}
    rel = [str((items.get(p) or {}).get("name")) for p in state.get("periods") or []
           if isinstance(items.get(p), dict) and items[p].get("name") and not re.match(r"^\d{4}", p)]
    if pe_names:
        span = pe_names[0] if len(pe_names) == 1 else f"{pe_names[0]} – {pe_names[-1]}"
        if rel and len(rel) == 1 and rel[0] != span:
            return f"{rel[0]} ({span})"
        return span
    return ", ".join(rel) or ", ".join(state.get("periods") or [])


async def build_map(state: AgentState, settings: Settings) -> dict:
    """Build the map payload or raise :class:`MapUnavailable` with an honest reason."""
    metrics = [m for m in state.get("metrics") or []
               if m.get("uid") and (DHIS2_UID_RE.match(m["uid"]) or REPORTING_RATE_RE.match(m["uid"]))]
    if not metrics:
        raise MapUnavailable("no data item was resolved for the map")
    has_credentials = bool(settings.dhis2_service_account_user or state.get("_dhis2_token"))
    if not has_credentials:
        raise MapUnavailable("maps need a live DHIS2 connection for boundaries, and none is configured")
    org = state.get("org_unit") or {}
    ou_dim = ou_dimension(state)
    if "LEVEL-" not in ou_dim:
        raise MapUnavailable(
            f"{org.get('label') or 'this place'} has no lower level to break down, so there is only one value to show")
    metric = metrics[0]
    token = state.get("_dhis2_token")
    from backend.app.dhis2.client import DHIS2Client
    client = DHIS2Client(settings, token=token)

    params = map_analytics_params(state, metric["uid"], ou_dim)
    try:
        raw = await client.analytics(params)
    except Exception as exc:
        logger.warning("map_analytics_failed", extra={"error": str(exc)[:300]})
        raise MapUnavailable("DHIS2 could not return one value per area for this period") from exc
    values, names, pe_names = parse_map_analytics(raw or {})
    if not values:
        raise MapUnavailable("DHIS2 returned no values per area for this period")

    try:
        geometry = await fetch_geometry(client, settings, ou_dim, token)
    except Exception as exc:
        logger.warning("map_geometry_failed", extra={"error": str(exc)[:300]})
        raise MapUnavailable("DHIS2 boundaries could not be loaded for these areas") from exc

    unit = await metric_unit(client, settings, metric, token)
    joined = join_values_to_geometry(values, names, geometry, unit)
    level_name = org.get("breakdown_level_name") or "area"
    if not joined["drawn_with_value"]:
        raise MapUnavailable(
            f"DHIS2 has no boundaries or coordinates for the {level_name.lower()} units in this answer")

    items = ((raw or {}).get("metaData") or {}).get("items") or {}
    period_label = _period_label(state, pe_names, items)
    place = org.get("label") or "National"
    if place.lower() in {"national", "federal"} or not DHIS2_UID_RE.match(org.get("uid") or ""):
        roots = {g.get("root") for g in geometry.values() if g.get("root")}
        root = next(iter(roots)) if len(roots) == 1 else ""
        place = str((items.get(root) or {}).get("name") or "") or "National"
    title = f"{metric.get('label')} by {level_name.lower()}, {place}, {period_label}"
    notes: list[str] = []
    if len(metrics) > 1:
        notes.append(f"The map shows the first item only ({metric.get('label')}); the table and chart data list the rest.")
    if joined["no_geometry"]:
        shown = joined["no_geometry"][:8]
        more = f" and {len(joined['no_geometry']) - 8} more" if len(joined["no_geometry"]) > 8 else ""
        notes.append(f"{len(joined['no_geometry'])} unit(s) with data have no boundary or coordinates in DHIS2 and are not drawn: "
                     + ", ".join(shown) + more + ".")
    if joined["no_value"]:
        notes.append(f"{len(joined['no_value'])} unit(s) have no data for this period (grey).")
    if len(state.get("periods") or []) > 1 or (pe_names and len(pe_names) > 1):
        notes.append("Each area shows one value for the whole period, as DHIS2 aggregates it (not a monthly value).")
    if [d for d in state.get("disaggregations") or [] if d != "ou_children"]:
        notes.append("The map shows totals, not the category breakdown.")

    table_rows = sorted(
        ([names.get(u, u), v] for u, v in values.items()),
        key=lambda r: (-(r[1] if r[1] is not None else -1e18), str(r[0]).lower()),
    )
    return {
        "id": f"map-{metric['uid']}-{abs(hash(ou_dim)) % 10_000_000}",
        "title": title,
        "metric": metric.get("label"),
        "metric_uid": metric["uid"],
        "unit": unit,
        "level_name": level_name,
        "period_label": period_label,
        "geojson": joined["geojson"],
        "points": joined["points"],
        "legend": joined["legend"],
        "no_data_color": NO_DATA_COLOR,
        "bbox": joined["bbox"],
        "notes": notes,
        "table": {
            "headers": ["Organisation unit", f"{metric.get('label')}{' (%)' if unit == '%' else ''}"],
            "rows": [[n, round(v, 2)] for n, v in table_rows],
        },
        "analytics_params": params,
    }


def export_payload_from_map(map_payload: dict, data_used: str = "") -> dict:
    """Rows/headers in the shape the XLSX/CSV export endpoints accept."""
    t = map_payload.get("table") or {}
    return {
        "title": map_payload.get("title") or "DHIS2 map data",
        "headers": t.get("headers") or ["Organisation unit", "Value"],
        "rows": t.get("rows") or [],
        "metadata": {"source": "DHIS2 analytics", "period": map_payload.get("period_label"),
                     "level": map_payload.get("level_name")},
        "sources": [],
    }
