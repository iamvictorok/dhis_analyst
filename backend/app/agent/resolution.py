"""Expert metric resolution: rank candidates, probe for data, judge the fit.

Replaces "take the top keyword hit" with a pipeline an implementer would trust:

1. **Rank** every data item in the synced catalogue (indicators, data elements,
   program indicators) against the user's own words *and* the parser's label,
   with IDF-weighted token overlap (rare words such as a disease or vaccine name
   count far more than "cases" or "doses"). Abbreviations are normalised
   ("Penta3" -> "penta 3", "ANC4" -> "anc 4", "4th" -> "4").
2. **Probe** the top candidates with ONE analytics call for the requested org
   unit and period, so we know which ones actually hold data.
3. **Judge**: the LLM (or a deterministic fallback) decides between
   ``match`` (pick one, keep up to 3 alternates for empty results),
   ``ambiguous`` (ask the user to choose among the top 3-5) and
   ``not_available`` (say plainly the concept is not in this instance and list
   the closest real items; never substitute silently).

Nothing here is specific to one DHIS2 instance or question.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from typing import Any

from backend.config import Settings

logger = logging.getLogger("dhis2_analyst.resolution")

DX_TYPES = ("indicator", "dataElement", "programIndicator")
TYPE_LABEL = {
    "indicator": "indicator",
    "dataElement": "data element",
    "programIndicator": "program indicator",
    "reportingRate": "data set reporting rate",
}

_ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
}

# Words that never identify a data item: question scaffolding, output formats,
# time words, geography words. (Generic DHIS2 vocabulary, not instance data.)
_STOP = frozenset("""
a an the of in on at for by to from and or vs versus with without per each every all any across among between
what which who how many much more than is are was were be been do does did show me give get list tell display
please can could would you i we our my us it its this that these those there their them
total number numbers count counts value values figure figures data stats statistics statistic trend trends
compare comparison comparing chart charts graph table tables dashboard report reports export excel csv
download pdf docx slide slides presentation map maps summary summarise summarize overview breakdown
broken down disaggregated disaggregate split top bottom highest lowest best worst most least rank ranked ranking
year years month months quarter quarters week weeks weekly monthly quarterly annual annually yearly day days
last this past previous current recent financial fiscal so far to date ytd since during over under
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec q
national country district districts region regions province provinces chiefdom chiefdoms facility facilities
level levels area areas org unit units organisation organization health centre center clinic clinics hospital
hospitals phu phus sub county counties state states lga ward wards zone zones
""".split())

# Words that occur in many data item names; they matter, but they rarely
# decide WHICH item is meant.
_GENERIC = frozenset("""
case dose given coverage rate visit reported report new total y year attendance service
percentage percent proportion ratio number indicator element program programme
""".split())

# Layperson / abbreviation expansions applied to the QUERY side only.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "antenatal": ("anc",), "prenatal": ("anc",), "anc": ("antenatal",),
    "immunisation": ("immunization",), "immunization": ("immunisation",),
    "vaccination": ("immunization", "dose"), "vaccine": ("dose", "immunization"),
    "shot": ("dose", "immunization"), "jab": ("dose", "immunization"),
    "dtp": ("penta",), "dpt": ("penta",), "pentavalent": ("penta",),
    "mcv": ("measles",), "polio": ("opv", "ipv"),
    "death": ("mortality", "dead", "deaths"), "mortality": ("death",), "died": ("death",),
    "tb": ("tuberculosis",), "tuberculosis": ("tb",),
    "diarrhea": ("diarrhoea",), "diarrhoea": ("diarrhea",),
    "pneumonia": ("ari",), "kid": ("child",), "children": ("child",), "baby": ("infant", "newborn"),
    "births": ("birth", "delivery"), "birth": ("delivery",), "delivery": ("birth",),
    "sex": ("gender",), "gender": ("sex",),
    "fic": ("fully", "immunized"),
    "llin": ("llitn", "net"), "itn": ("llitn", "net"), "net": ("llin", "llitn"),
    "ipt": ("ipt",), "dropout": ("drop", "dropout"),
    "completeness": ("reporting",),
}


def tokens(text: str) -> list[str]:
    """Normalise a phrase into comparable tokens.

    "Penta3" -> penta 3; "ANC 4th" -> anc 4; "<1y" -> 1 y; plural "doses" -> dose.
    """
    t = (text or "").lower()
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    out = []
    for w in re.findall(r"[a-z]+|\d+", t):
        w = _ORDINALS.get(w, w)
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.append(w)
    return out


def content_tokens(text: str, extra_stop: set[str] | None = None) -> list[str]:
    stop = _STOP | (extra_stop or set())
    seen, out = set(), []
    for w in tokens(text):
        if w in stop or (len(w) < 2 and not w.isdigit()):
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# Catalogue (cached)
# ---------------------------------------------------------------------------

_CATALOGUE: dict[str, Any] = {"expires": 0.0, "items": [], "idf": {}, "vocab": set(), "ou_tokens": set()}
_CATALOGUE_TTL = 600


def invalidate_catalogue() -> None:
    _CATALOGUE["expires"] = 0.0


async def load_catalogue(settings: Settings) -> dict[str, Any]:
    now = time.time()
    if _CATALOGUE["items"] and _CATALOGUE["expires"] > now:
        return _CATALOGUE
    from sqlalchemy import text
    from backend.app.db.session import get_db_session

    table = "metadata_index" if settings.is_postgres else "metadata_index_fts"
    async with get_db_session() as session:
        rows = (await session.execute(text(
            f"SELECT uid, name, short_name, object_type, description FROM {table} "
            "WHERE object_type IN ('indicator','dataElement','programIndicator','orgUnit')"
        ))).fetchall()
    items, df, ou_tokens = [], {}, set()
    for uid, name, short, otype, desc in rows:
        name = " ".join(str(name or "").split())
        if otype == "orgUnit":
            ou_tokens.update(tokens(name))
            continue
        toks = set(tokens(name)) | set(tokens(short or ""))
        if not toks:
            continue
        items.append({"uid": uid, "label": name, "object_type": otype,
                      "description": (desc or "")[:200], "tokens": toks,
                      "name_tokens": tokens(name)})
        for w in toks:
            df[w] = df.get(w, 0) + 1
    n = max(len(items), 1)
    idf = {w: math.log((n + 1) / (c + 1)) + 1.0 for w, c in df.items()}
    _CATALOGUE.update(items=items, idf=idf, vocab=set(df), ou_tokens=ou_tokens,
                      expires=now + _CATALOGUE_TTL, max_idf=math.log(n + 1) + 1.0)
    return _CATALOGUE


def _weight(w: str, cat: dict) -> float:
    base = cat["idf"].get(w, cat.get("max_idf", 8.0))
    return base * (0.35 if w in _GENERIC else 1.0)


def score_item(q: list[str], item: dict, cat: dict) -> float:
    """IDF-weighted overlap of query tokens with an item's tokens (0..~1.1)."""
    if not q:
        return 0.0
    itoks = item["tokens"]
    total = matched = 0.0
    for w in q:
        wt = _weight(w, cat)
        total += wt
        alts = (w,) + _SYNONYMS.get(w, ())
        if any(a in itoks for a in alts):
            matched += wt
    if matched == 0:
        return 0.0
    qcov = matched / total
    qset = set(q)
    for w in q:
        qset.update(_SYNONYMS.get(w, ()))
    name_toks = [w for w in item["name_tokens"] if w not in _STOP] or item["name_tokens"]
    ntot = sum(_weight(w, cat) for w in name_toks) or 1.0
    nmatch = sum(_weight(w, cat) for w in name_toks if w in qset)
    ncov = nmatch / ntot
    s = 0.6 * qcov + 0.4 * ncov
    # Whole name appears in order inside the query ("penta 3 coverage 1 y").
    nt = " ".join(item["name_tokens"])
    if nt and f" {nt} " in f" {' '.join(q)} ":
        s += 0.1 * qcov
    return round(s, 4)


def unknown_tokens(q: list[str], cat: dict) -> list[str]:
    """Distinctive query words that appear in NO data item name of this instance."""
    out = []
    for w in q:
        if w.isdigit() or w in _GENERIC or w in cat["ou_tokens"] or len(w) < 3:
            continue
        alts = (w,) + _SYNONYMS.get(w, ())
        if not any(a in cat["vocab"] for a in alts):
            out.append(w)
    return out


def rank_candidates(queries: list[tuple[list[str], float]], cat: dict, limit: int = 12,
                    per_family: int = 3) -> list[dict]:
    """Best-scoring items across query variants, with near-duplicates thinned.

    A "family" is the name before " - " or "(" plus the type, so dozens of
    "<measure> - <age/sex/method variant>" program indicators count as one
    family and do not crowd out a differently named item that may be the one
    holding data.
    """
    best: dict[str, dict] = {}
    for q, weight in queries:
        if not q:
            continue
        qset = set(q)
        for it in cat["items"]:
            s = score_item(q, it, cat) * weight
            if s <= 0:
                continue
            cur = best.get(it["uid"])
            if cur is None or s > cur["score"]:
                sig = re.split(r"\s+-\s+|\s*\(", it["label"].lower())[0].strip()
                best[it["uid"]] = {"uid": it["uid"], "label": it["label"],
                                   "object_type": it["object_type"],
                                   "description": it["description"], "score": round(s, 4),
                                   "_family": (sig, it["object_type"])}
    ranked = sorted(best.values(), key=lambda c: (-c["score"], len(c["label"]), c["uid"]))
    out, fam_count = [], {}
    for c in ranked:
        if c["score"] < 0.12:
            break
        n = fam_count.get(c["_family"], 0)
        if n >= per_family:
            continue
        fam_count[c["_family"]] = n + 1
        out.append({k: v for k, v in c.items() if k != "_family"})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Data availability probe
# ---------------------------------------------------------------------------

async def probe_data(client, uids: list[str], ou: str, pe: str, max_strip: int = 3) -> dict[str, float | None] | None:
    """One analytics call over many dx items: which hold data for this ou/pe?

    Returns {uid: total or None}; None when the probe itself could not run (e.g.
    cached snapshot mode) so callers treat availability as unknown.
    """
    remaining = list(dict.fromkeys(uids))
    excluded: list[str] = []
    for _ in range(max_strip + 1):
        if not remaining:
            break
        params = {
            "dimension": [f"dx:{';'.join(remaining)}"],
            "filter": [f"ou:{ou}", f"pe:{pe}"],
            "skipMeta": "true",
        }
        try:
            raw = await client.analytics(params)
        except Exception as exc:
            msg = _error_text(exc)
            bad = re.findall(r"`?\[?([A-Za-z][A-Za-z0-9]{10})\]?`?", msg) if "E7115" in msg or "aggregation" in msg.lower() else []
            bad = [b for b in bad if b in remaining]
            if bad:
                excluded += bad
                remaining = [u for u in remaining if u not in bad]
                continue
            logger.info("probe_failed", extra={"error": msg[:300]})
            return None
        out: dict[str, float | None] = {u: None for u in uids}
        for row in raw.get("rows") or []:
            try:
                out[row[0]] = float(row[-1])
            except (TypeError, ValueError, IndexError):
                continue
        for u in excluded:
            out[u] = None
        return out
    return None


def _error_text(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if resp is not None and hasattr(resp, "text"):
        try:
            return f"HTTP {resp.status_code} {resp.text}"
        except Exception:
            pass
    return str(exc)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You are a DHIS2 implementer choosing which data item answers a user's question.
Only the candidates below exist in this DHIS2 instance; nothing else does.

{context}User question: "{question}"
Concept the parser extracted: "{label}"
Place: {place}. Period: {period}.

Candidates (name [type] - data for this place/period):
{candidates}

Decide:
- "match": one candidate measures what the user asked for. Treat abbreviations, misspellings and
  naming conventions as the same concept (e.g. Penta3 = Penta 3, ANC4 = ANC 4th visit, "under one" = <1y).
  If the user asks for coverage, a rate, a percentage or a dropout, prefer an indicator; for counts,
  doses, cases or visits prefer a data element. Prefer candidates that have data. Prefer routine
  aggregate data elements/indicators over tracker program indicators unless the user mentions a
  programme, tracker, case-based or individual-level data. Pick the overall total rather than a
  sex/age-specific variant unless the user asked for that variant.
- "ambiguous": the question names only a broad area (for example just "coverage", "immunisation",
  "maternal health", "malaria data") without saying which measure, and several different candidates
  fit about equally. Variants of one measure are not ambiguity.
- "not_available": the specific disease, vaccine, service, programme or measure the user names is not
  what any candidate measures. A candidate about a different disease, vaccine or service is NOT a match,
  even if it shares words such as "doses", "coverage" or "cases". Never substitute.

Return ONLY JSON:
{{"decision": "match|ambiguous|not_available", "choice": <candidate number or 0>,
  "alternatives": [<other candidates measuring the same concept, best first, max 3>],
  "options": [<3-5 candidate numbers to offer the user when ambiguous or not_available>],
  "requested_concept": "<what the user asked for, in a few words>", "reason": "<one short sentence>"}}"""


def diversify(cands: list[dict], n: int, keep_top: int = 3) -> list[dict]:
    """Top ``keep_top`` by score, then round-robin across name families (first word)
    so a broad question shows ANC, BCG, measles... rather than five ANC variants."""
    out = list(cands[:keep_top])
    groups: dict[str, list[dict]] = {}
    for c in cands[keep_top:]:
        key = (tokens(c["label"]) or [""])[0]
        groups.setdefault(key, []).append(c)
    seen_first = {(tokens(c["label"]) or [""])[0] for c in out}
    order = sorted(groups, key=lambda k: (k in seen_first, -groups[k][0]["score"]))
    while len(out) < n and any(groups.values()):
        for k in order:
            if groups[k] and len(out) < n:
                out.append(groups[k].pop(0))
    return out


def _fmt_candidates(cands: list[dict]) -> str:
    lines = []
    for i, c in enumerate(cands, 1):
        has = c.get("has_data")
        if has is None:
            avail = "data: unknown"
        elif has:
            tot = c.get("data_total")
            avail = f"has data (total {tot:,.0f})" if isinstance(tot, (int, float)) else "has data"
        else:
            avail = "NO data"
        desc = f" ({c['description'][:90]})" if c.get("description") else ""
        lines.append(f"{i}. \"{c['label']}\" [{TYPE_LABEL.get(c['object_type'], c['object_type'])}] - {avail}{desc}")
    return "\n".join(lines)


async def llm_judge(question: str, label: str, cands: list[dict], place: str, period: str,
                    context_msgs: list[str], settings: Settings) -> dict | None:
    from backend.app.llm import complete

    ctx = ""
    if context_msgs:
        ctx = "Earlier user messages in this conversation (use them if the question is a follow-up):\n" + \
              "\n".join(f"- \"{m[:200]}\"" for m in context_msgs[-3:]) + "\n\n"
    prompt = JUDGE_PROMPT.format(context=ctx, question=question[:500], label=label[:120],
                                 candidates=_fmt_candidates(cands), place=place, period=period)
    try:
        raw = await complete([{"role": "user", "content": prompt}], settings, json_mode=True, temperature=0.0)
        m = re.search(r"\{.*\}", raw or "", flags=re.S)
        parsed = json.loads(m.group(0) if m else raw)
    except Exception as exc:
        logger.warning("llm_judge_failed", extra={"error": str(exc)[:300]})
        return None
    if not isinstance(parsed, dict) or parsed.get("decision") not in {"match", "ambiguous", "not_available"}:
        return None

    def idx_list(v) -> list[int]:
        out = []
        for x in (v if isinstance(v, list) else []):
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(cands) and i not in out:
                out.append(i)
        return out

    try:
        choice = int(parsed.get("choice") or 0)
    except (TypeError, ValueError):
        choice = 0
    if parsed["decision"] == "match" and not (1 <= choice <= len(cands)):
        return None
    return {
        "decision": parsed["decision"],
        "choice": choice,
        "alternatives": [i for i in idx_list(parsed.get("alternatives")) if i != choice],
        "options": idx_list(parsed.get("options")),
        "requested_concept": str(parsed.get("requested_concept") or label)[:80],
        "reason": str(parsed.get("reason") or "")[:200],
        "by": "llm",
    }


def deterministic_judge(q_user: list[str], cands: list[dict], unknown: list[str]) -> dict:
    """Fallback when no LLM is available: conservative thresholds."""
    if not cands:
        return {"decision": "not_available", "choice": 0, "alternatives": [], "options": [], "by": "rules"}
    # Fit to the user's words decides; having data and being an aggregate item
    # only break near-ties (a broader item must not beat a precise one).
    ranked = sorted(range(len(cands)), key=lambda i: -(
        cands[i]["score"]
        + (0.03 if cands[i].get("has_data") else 0)
        + (0.02 if cands[i].get("object_type") != "programIndicator" else 0)
    ))
    top = cands[ranked[0]]["score"]
    if top < 0.35 or (unknown and top < 0.75):
        return {"decision": "not_available", "choice": 0, "alternatives": [],
                "options": [], "by": "rules"}
    specific = [w for w in q_user if w not in _GENERIC and not w.isdigit()]
    close = [i for i in ranked if cands[i]["score"] >= top - 0.05]
    if not specific and len(close) >= 3:
        return {"decision": "ambiguous", "choice": 0, "alternatives": [],
                "options": [], "by": "rules"}
    return {"decision": "match", "choice": ranked[0] + 1,
            "alternatives": [i + 1 for i in ranked[1:4]], "options": [], "by": "rules"}


# ---------------------------------------------------------------------------
# Org-unit levels and breakdown parsing
# ---------------------------------------------------------------------------

_LEVELS_CACHE: dict[str, Any] = {"expires": 0.0, "levels": None}

_FACILITY_WORDS = r"(?:health\s+)?(?:facilit(?:y|ies)|clinics?|hospitals?|phus?|health\s+cent(?:re|er)s?|health\s+posts?)"


async def org_levels(settings: Settings, token: str | None = None) -> list[dict] | None:
    now = time.time()
    if _LEVELS_CACHE["levels"] and _LEVELS_CACHE["expires"] > now:
        return _LEVELS_CACHE["levels"]
    try:
        from backend.app.dhis2.client import DHIS2Client
        raw = await DHIS2Client(settings, token=token).get(
            "/api/organisationUnitLevels.json", params={"fields": "level,name", "paging": "false"})
        levels = sorted(
            [{"level": int(l["level"]), "name": str(l["name"])} for l in raw.get("organisationUnitLevels") or []
             if l.get("level") and l.get("name")],
            key=lambda l: l["level"])
        if levels:
            _LEVELS_CACHE.update(levels=levels, expires=now + 3600)
            return levels
    except Exception as exc:
        logger.info("org_levels_unavailable", extra={"error": str(exc)[:200]})
    return None


def _plural(word: str) -> str:
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy")):
        return word[:-1] + "ies"
    return word + "s"


def parse_breakdown_level(message: str, levels: list[dict] | None) -> int | None:
    """Level the user wants rows for: "by district", "each chiefdom", "facilities in Bo"."""
    if not message or not levels:
        return None
    low = " ".join(message.lower().split())
    explicit: list[int] = []
    implicit: list[int] = []
    for lv in levels:
        if lv["level"] <= 1:
            continue
        lname = lv["name"].lower()
        name = re.escape(lname)
        plural = re.escape(_plural(lname))
        term = rf"(?:{name}|{plural})"
        is_facility = "facilit" in lname
        if is_facility:
            term = rf"(?:{term}|{_FACILITY_WORDS})"
        explicit_pats = [
            rf"\b(?:by|per|each|every|across|among|for\s+each|for\s+every|for\s+all|all)\s+(?:the\s+)?(?:\w+\s+)?{term}\b",
            rf"\b{term}[\s-]+level\b",
            rf"\b(?:top|bottom|best|worst|highest|lowest)\s+(?:\d+\s+|three\s+|five\s+|ten\s+)?{term}\b",
            rf"\b(?:which|what)\s+(?:\w+\s+)?{term}\b",
        ]
        implicit_pats = [rf"\b{plural}\b"]
        if is_facility:
            implicit_pats.append(r"\b(?:facilities|clinics|hospitals|phus|health\s+cent(?:re|er)s|health\s+posts)\b")
        if any(re.search(p, low) for p in explicit_pats):
            explicit.append(lv["level"])
        elif any(re.search(p, low) for p in implicit_pats):
            implicit.append(lv["level"])
    if explicit:
        return max(explicit)
    if implicit:
        return max(implicit)
    return None


def level_name(level: int | None, levels: list[dict] | None) -> str:
    if not level:
        return ""
    for lv in levels or []:
        if lv["level"] == level:
            return lv["name"]
    return f"level {level}"
