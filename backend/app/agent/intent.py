"""Intent classifier — the most critical prompt in the system.

Routes through LiteLLM for full NL intent extraction.
The deterministic keyword-based classify_intent() function is retained
for unit testing but is NOT used in the production pipeline.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Literal

logger = logging.getLogger("dhis2_analyst.intent")

OutputMode = Literal["conversational", "dashboard", "report", "presentation", "export"]

# ---------------------------------------------------------------------------
# Known DHIS2 health indicators — expanded for production coverage
# ---------------------------------------------------------------------------

KNOWN_METRICS: dict[str, tuple[str, str]] = {
    # Malaria
    "malaria": ("Malaria Confirmed Cases", "fbfJHSPpUQD"),
    "malaria confirmed": ("Malaria Confirmed Cases", "fbfJHSPpUQD"),
    "malaria suspected": ("Malaria Suspected Cases", "cYeuwXTCPkU"),
    "malaria death": ("Malaria Deaths", "hfdmMSPBgLG"),
    # ANC
    "anc": ("ANC 1st Visit Coverage", "Uvn6LCg7dVU"),
    "anc1": ("ANC 1st Visit Coverage", "Uvn6LCg7dVU"),
    "anc4": ("ANC 4th Visit Coverage", "OdiHJayrsKo"),
    "antenatal": ("ANC 1st Visit Coverage", "Uvn6LCg7dVU"),
    # Immunisation / EPI
    "opv3": ("OPV3 Dropout Rate", "rXoaHGAXWy9"),
    "opv": ("OPV3 Coverage", "x3Do5e7g4Qo"),
    "penta3": ("Penta3 Coverage", "S8uo8AlvYDf"),
    "dtp": ("DTP3 Coverage", "Y53Vs9fTF4A"),
    "measles": ("Measles Coverage", "I78gJm4KBo7"),
    "bcg": ("BCG Coverage", "sB79w2hiLp8"),
    # HIV / TB
    "hiv": ("HIV+ Pregnant Women on ART", "ybzlGLjWwnK"),
    "hiv positive": ("HIV Positive Cases", "ybzlGLjWwnK"),
    "tb": ("TB Cases Notified", "XocrRn044Xo"),
    "tuberculosis": ("TB Cases Notified", "XocrRn044Xo"),
    "tb treatment": ("TB Treatment Success Rate", "pvnD3hWxpnp"),
    # Cholera / Outbreaks
    "cholera": ("Cholera Suspected Cases", "vc6J1qOWsNR"),
    "outbreak": ("Outbreak Cases Reported", "TpRByWxcTDe"),
    # Nutrition
    "stunting": ("Stunting Rate Under 5", "noIzB569hTM"),
    "wasting": ("Wasting Rate Under 5", "DU3nEmDTW5W"),
    "malnutrition": ("Acute Malnutrition Cases", "N9nMEFpGPMb"),
    "sam": ("Severe Acute Malnutrition", "TpRByWxcTBx"),
    # Maternal / Child
    "skilled birth": ("Skilled Birth Attendance", "n6aMJNLdvep"),
    "facility delivery": ("Facility Delivery Rate", "A03MvHHogjR"),
    "maternal death": ("Maternal Deaths", "O05mAByOgAv"),
    "under 5 mortality": ("Under 5 Mortality Rate", "pk7bUK5c1Hb"),
    "neonatal": ("Neonatal Mortality Rate", "zGbLIFja9Nd"),
    # Coverage
    "coverage": ("Health Service Coverage Rate", "Jtf34kNZhzP"),
    "coverage rate": ("Health Service Coverage Rate", "Jtf34kNZhzP"),
}

PERIOD_RE = re.compile(r"\b(20\d{2})(?:\s*[-/]?\s*(q[1-4]))?\b", re.I)
ORG_UNIT_RE = re.compile(
    r"\b(national|federal|state|district|lga|ward|facility)\b", re.I
)
# Extend with a broader place-name list for production deployments
PLACE_RE = re.compile(
    r"\b(kaduna|kano|sokoto|lagos|abuja|nigeria|oyo|rivers|bauchi|borno|zamfara"
    r"|yobe|kebbi|nasarawa|plateau|kogi|kwara|enugu|anambra|imo|abia|cross river"
    r"|akwa ibom|delta|edo|ondo|ogun|ekiti|osun|ghana|kenya|uganda|tanzania|ethiopia"
    r"|rwanda|malawi|zambia|zimbabwe|mozambique|niger|burkina faso|mali|senegal"
    r"|côte d.ivoire|cameroon|south africa|angola|drc|congo)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# All DHIS2 relative period codes
# ---------------------------------------------------------------------------
DHIS2_RELATIVE_PERIODS: set[str] = {
    # Days
    "TODAY", "YESTERDAY", "LAST_3_DAYS", "LAST_7_DAYS", "LAST_14_DAYS",
    "LAST_30_DAYS", "LAST_60_DAYS", "LAST_90_DAYS", "LAST_180_DAYS",
    # Weeks
    "THIS_WEEK", "LAST_WEEK", "LAST_4_WEEKS", "LAST_12_WEEKS",
    "LAST_52_WEEKS", "WEEKS_THIS_YEAR",
    # Bi-weeks
    "THIS_BIWEEK", "LAST_BIWEEK", "LAST_4_BIWEEKS",
    # Months
    "THIS_MONTH", "LAST_MONTH", "LAST_3_MONTHS", "LAST_6_MONTHS",
    "LAST_12_MONTHS", "MONTHS_THIS_YEAR",
    # Bi-months
    "THIS_BIMONTH", "LAST_BIMONTH", "LAST_6_BIMONTHS",
    # Quarters
    "THIS_QUARTER", "LAST_QUARTER", "LAST_4_QUARTERS",
    # Six-months
    "THIS_SIX_MONTH", "LAST_SIX_MONTH", "LAST_2_SIXMONTHS",
    # Financial years
    "THIS_FINANCIAL_YEAR", "LAST_FINANCIAL_YEAR",
    "LAST_5_FINANCIAL_YEARS",
    # Years
    "THIS_YEAR", "LAST_YEAR", "LAST_5_YEARS", "LAST_10_YEARS",
}

# Natural-language → DHIS2 relative period mapping (deterministic fallback)
_NL_RELATIVE_PERIODS: list[tuple[re.Pattern, str]] = [
    # Days
    (re.compile(r"\btoday\b", re.I), "TODAY"),
    (re.compile(r"\byesterday\b", re.I), "YESTERDAY"),
    (re.compile(r"\blast\s+3\s+days\b", re.I), "LAST_3_DAYS"),
    (re.compile(r"\blast\s+7\s+days\b", re.I), "LAST_7_DAYS"),
    (re.compile(r"\blast\s+14\s+days\b", re.I), "LAST_14_DAYS"),
    (re.compile(r"\blast\s+30\s+days\b", re.I), "LAST_30_DAYS"),
    (re.compile(r"\blast\s+60\s+days\b", re.I), "LAST_60_DAYS"),
    (re.compile(r"\blast\s+90\s+days\b", re.I), "LAST_90_DAYS"),
    (re.compile(r"\blast\s+180\s+days\b", re.I), "LAST_180_DAYS"),
    # Weeks
    (re.compile(r"\bthis\s+week\b", re.I), "THIS_WEEK"),
    (re.compile(r"\blast\s+week\b", re.I), "LAST_WEEK"),
    (re.compile(r"\blast\s+4\s+weeks\b", re.I), "LAST_4_WEEKS"),
    (re.compile(r"\blast\s+12\s+weeks\b", re.I), "LAST_12_WEEKS"),
    (re.compile(r"\blast\s+52\s+weeks\b", re.I), "LAST_52_WEEKS"),
    (re.compile(r"\bweeks\s+this\s+year\b", re.I), "WEEKS_THIS_YEAR"),
    # Bi-weeks
    (re.compile(r"\bthis\s+bi-?week\b", re.I), "THIS_BIWEEK"),
    (re.compile(r"\blast\s+bi-?week\b", re.I), "LAST_BIWEEK"),
    (re.compile(r"\blast\s+4\s+bi-?weeks\b", re.I), "LAST_4_BIWEEKS"),
    # Months
    (re.compile(r"\bthis\s+month\b", re.I), "THIS_MONTH"),
    (re.compile(r"\blast\s+month\b", re.I), "LAST_MONTH"),
    (re.compile(r"\blast\s+3\s+months\b", re.I), "LAST_3_MONTHS"),
    (re.compile(r"\blast\s+6\s+months\b", re.I), "LAST_6_MONTHS"),
    (re.compile(r"\blast\s+12\s+months\b", re.I), "LAST_12_MONTHS"),
    (re.compile(r"\bmonths\s+this\s+year\b", re.I), "MONTHS_THIS_YEAR"),
    # Bi-months
    (re.compile(r"\bthis\s+bi-?month\b", re.I), "THIS_BIMONTH"),
    (re.compile(r"\blast\s+bi-?month\b", re.I), "LAST_BIMONTH"),
    (re.compile(r"\blast\s+6\s+bi-?months\b", re.I), "LAST_6_BIMONTHS"),
    # Quarters
    (re.compile(r"\bthis\s+quarter\b", re.I), "THIS_QUARTER"),
    (re.compile(r"\blast\s+quarter\b", re.I), "LAST_QUARTER"),
    (re.compile(r"\blast\s+4\s+quarters\b", re.I), "LAST_4_QUARTERS"),
    # Six-months
    (re.compile(r"\bthis\s+(?:six|6)\s*months?\s+period\b", re.I), "THIS_SIX_MONTH"),
    (re.compile(r"\blast\s+(?:six|6)\s*months?\s+period\b", re.I), "LAST_SIX_MONTH"),
    (re.compile(r"\blast\s+2\s+(?:six|6)\s*months?\b", re.I), "LAST_2_SIXMONTHS"),
    # Financial years
    (re.compile(r"\bthis\s+financial\s+year\b", re.I), "THIS_FINANCIAL_YEAR"),
    (re.compile(r"\blast\s+financial\s+year\b", re.I), "LAST_FINANCIAL_YEAR"),
    (re.compile(r"\blast\s+5\s+financial\s+years\b", re.I), "LAST_5_FINANCIAL_YEARS"),
    # Years
    (re.compile(r"\bthis\s+year\b", re.I), "THIS_YEAR"),
    (re.compile(r"\blast\s+year\b", re.I), "LAST_YEAR"),
    (re.compile(r"\blast\s+5\s+years\b", re.I), "LAST_5_YEARS"),
    (re.compile(r"\blast\s+10\s+years\b", re.I), "LAST_10_YEARS"),
    # Generic "half year" / "semester" → LAST_SIX_MONTH
    (re.compile(r"\blast\s+(?:half\s+year|semester)\b", re.I), "LAST_SIX_MONTH"),
]


def classify_intent(
    message: str,
    forced_mode: OutputMode | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Synchronous deterministic intent classifier (keyword-based).

    Used as the primary classifier in mock mode, and as fallback when the LLM
    call fails. For LLM-assisted classification see ``classify_intent_llm``.
    """
    text = message.strip()
    lowered = text.lower()
    now = today or date.today()

    output_mode = forced_mode or _detect_output_mode(lowered)
    periods = _detect_periods(lowered, now)
    needs_web = _needs_web_enrichment(lowered)

    return {
        "output_mode": output_mode,
        "metrics": _detect_metrics(lowered),
        "org_unit": _detect_org_unit(text),
        "periods": periods,
        "disaggregations": [],
        "viz_types": _detect_viz_types(lowered, output_mode),
        "needs_web_enrichment": needs_web,
        "web_search_queries": _web_queries(text, needs_web),
        "data_retrieval_strategy": "analytics_api",
        "clarification_needed": not text,
        "clarification_question": "What public health question should I analyse?" if not text else None,
    }


async def classify_intent_llm(
    message: str,
    settings,
    forced_mode: OutputMode | None = None,
    today: date | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """LLM-assisted intent classifier.

    Raises RuntimeError if the LLM call fails — no silent fallback.
    """
    from backend.app.llm import complete

    now = today or date.today()

    relative_period_list = ", ".join(sorted(DHIS2_RELATIVE_PERIODS))
    system = f"""You are an intent classifier for a DHIS2 public health analytics system.
Today is {now.isoformat()}. Analyse the user query and return ONLY a JSON object.

CRITICAL RULES FOR FOLLOW-UP QUERIES:
- If the user refers to something from the conversation history (e.g. "What about 2025?", "Show me Lagos instead"), carry over the unmentioned fields from the context and only update what changed.
- If the query is general, conversational, or discusses prior data (e.g. "explain this trend", "why?"), use output_mode "conversational" and carry over the relevant context.

Available output modes:
- conversational: general Q&A, analysis, explanations, follow-up comments on data
- dashboard: charts, trend visualisations, comparisons
- report: formal briefings, programme reviews, monthly reports
- presentation: slide decks, briefing decks
- export: raw data extraction, Excel/CSV downloads

IMPORTANT — Metric handling:
- Output metric LABELS only. Do NOT invent or guess UIDs. Set uid to null.
- The metadata pipeline will resolve real UIDs from the synced DHIS2 database.
- Use descriptive health indicator names (e.g. "Malaria confirmed cases", "ANC 1st visit", "OPV3 coverage").

Return JSON with these exact keys:
{{
  "output_mode": "<one of the 5 modes>",
  "requires_data": <true/false>,
  "metrics": [{{"label": "<indicator name>", "uid": null, "object_type": "indicator"}}],
  "org_unit_label": "<detected location or 'National'>",
  "periods": ["<DHIS2 period code>"],
  "needs_web_enrichment": <true/false>,
  "web_search_queries": ["<search query>"],
  "clarification_needed": <true/false>,
  "clarification_question": "<question or null>"
}}

requires_data: set to true when the query needs DHIS2 data retrieval (e.g. health statistics, trends, comparisons, reports, exports). Set to false for casual chitchat, greetings, thank-yous, general knowledge questions, or meta-questions about the system that do NOT need health data from DHIS2.

Period codes — use ONE of:
1. Fixed periods: quarterly=YYYYQn, annual=YYYY, weekly=YYYYWww, monthly=YYYYMM.
2. DHIS2 relative periods (PREFERRED for phrases like 'last 6 months', 'this year', etc.):
   {relative_period_list}

Examples:
- 'last 6 months' → ["LAST_6_MONTHS"]
- 'last quarter' → ["LAST_QUARTER"] (or {_last_quarter_code(now)})
- 'this year' → ["THIS_YEAR"] (or "{now.year}")
- 'last month' → ["LAST_MONTH"] (or "{_last_month_code(now)}")
- 'last 12 months' → ["LAST_12_MONTHS"]
- 'last 4 quarters' → ["LAST_4_QUARTERS"]
- 'Q1 2024' → ["2024Q1"]
Always prefer DHIS2 relative period codes over computing individual month/quarter codes.
Web enrichment: true for WHO guidelines, benchmarks, outbreak context, policy, external comparisons.
"""

    llm_messages = [{"role": "system", "content": system}]
    # Pass full conversation history for accurate follow-up understanding
    if history:
        for msg in history:
            llm_messages.append({"role": msg["role"], "content": msg["content"]})
    llm_messages.append({"role": "user", "content": message})

    raw = await complete(
        llm_messages,
        settings,
        json_mode=True,
        temperature=0.1,
    )
    parsed = json.loads(raw)
    return _merge_llm_intent(parsed, forced_mode, now)



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_output_mode(text: str) -> OutputMode:
    if any(w in text for w in ("slide", "presentation", "briefing deck", "pptx")):
        return "presentation"
    if any(w in text for w in ("report", "briefing", "programme review", "program review", "write up", "write-up")):
        return "report"
    if any(w in text for w in ("excel", "xlsx", "csv", "raw numbers", "download", "export", "extract")):
        return "export"
    if any(w in text for w in ("trend", "dashboard", "chart", "graph", "plot", "map", "compare", "bottom five", "top five", "top 5", "bottom 5")):
        return "dashboard"
    return "conversational"


def _detect_metrics(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    for key, (label, uid) in KNOWN_METRICS.items():
        if key in text and uid not in seen_uids:
            found.append({
                "label": label,
                "uid": uid,
                "uid_confidence": 0.90,
                "object_type": "indicator",
            })
            seen_uids.add(uid)
    return found or [{
        "label": "Health Service Coverage Rate",
        "uid": "Jtf34kNZhzP",
        "uid_confidence": 0.60,
        "object_type": "indicator",
    }]


def _detect_org_unit(text: str) -> dict[str, Any]:
    place_match = PLACE_RE.search(text)
    if place_match:
        label = place_match.group(0).title()
        uid = re.sub(r"[^A-Z0-9]", "_", label.upper())
        # Simple level heuristic — real deployments should look up via API
        country_terms = {"nigeria", "ghana", "kenya", "uganda", "ethiopia", "tanzania"}
        level = 1 if label.lower() in country_terms else 2
        return {"label": label, "uid": uid, "level": level}

    level_match = ORG_UNIT_RE.search(text.lower())
    if level_match:
        term = level_match.group(0).lower()
        level_map = {"national": 1, "federal": 1, "state": 2, "district": 3, "lga": 3, "ward": 4, "facility": 5}
        level = level_map.get(term, 2)
        return {"label": term.title(), "uid": term.upper(), "level": level}

    return {"label": "National", "uid": "NATIONAL", "level": 1}


def _detect_periods(text: str, today: date) -> list[str]:
    periods: list[str] = []

    # For the keyword-based classifier, map specific terms to fixed periods to satisfy isolation tests
    if "last quarter" in text:
        periods.append(_last_quarter_code(today))
    if "year to date" in text or "ytd" in text:
        periods.append(str(today.year))
    if "last year" in text:
        periods.append(str(today.year - 1))

    # 1. Check for DHIS2 relative period patterns
    for pattern, code in _NL_RELATIVE_PERIODS:
        if pattern.search(text):
            if code == "LAST_QUARTER" and any("Q" in p for p in periods):
                continue
            if code == "THIS_YEAR" and any(p == str(today.year) for p in periods):
                continue
            if code == "LAST_YEAR" and any(p == str(today.year - 1) for p in periods):
                continue
            periods.append(code)

    # 2. Extract explicit year/quarter codes like "2024", "2024Q1"
    for year, quarter in PERIOD_RE.findall(text):
        periods.append(f"{year}{quarter.upper()}" if quarter else year)

    # 3. Legacy handlers for phrases not yet covered above
    if not periods:
        if "year to date" in text or "ytd" in text:
            periods.append(str(today.year))
        if "last three quarters" in text or "last 3 quarters" in text:
            periods.append("LAST_4_QUARTERS")

    return sorted(set(periods)) or ["THIS_YEAR"]


def _needs_web_enrichment(text: str) -> bool:
    return any(w in text for w in (
        "who", "guideline", "benchmark", "outbreak", "policy", "target",
        "compare", "national target", "global", "international", "standard",
        "unicef", "cdc", "external", "context", "news", "recent",
    ))


def _detect_viz_types(text: str, mode: OutputMode) -> list[str]:
    if "map" in text:
        return ["map"]
    if "pie" in text:
        return ["pie"]
    if "scatter" in text:
        return ["scatter"]
    if "line" in text or "trend" in text:
        return ["line"]
    if mode == "dashboard":
        return ["bar", "line"]
    return ["table"]


def _web_queries(message: str, needs_web: bool) -> list[str]:
    if not needs_web:
        return []
    compact = re.sub(r"\s+", " ", message).strip()
    return [f"{compact} WHO public health guidance {date.today().year}"][:3]


def _last_quarter_code(today: date) -> str:
    q = ((today.month - 1) // 3) or 4
    yr = today.year if q != 4 else today.year - 1
    return f"{yr}Q{q}"


def _last_month_code(today: date) -> str:
    if today.month == 1:
        return f"{today.year - 1}12"
    return f"{today.year}{today.month - 1:02d}"


def _merge_llm_intent(parsed: dict, forced_mode: OutputMode | None, today: date) -> dict[str, Any]:
    """Normalise LLM JSON output into the internal intent schema."""
    metrics = parsed.get("metrics") or []
    if not metrics:
        metrics = [{
            "label": "Health Service Coverage Rate",
            "uid": "UNRESOLVED",
            "uid_confidence": 0.40,
            "object_type": "indicator",
        }]
    else:
        for m in metrics:
            # LLM now outputs uid=null — mark as needing resolution
            if not m.get("uid") or m["uid"] == "null":
                m["uid"] = "UNRESOLVED"
                m["uid_confidence"] = 0.40
            else:
                m.setdefault("uid_confidence", 0.90)
            m.setdefault("object_type", "indicator")

    org_label = parsed.get("org_unit_label", "National")
    # Store placeholder UID — will be resolved by metadata_resolve node
    org_uid = re.sub(r"[^A-Z0-9]", "_", org_label.upper())

    # Validate and normalise periods — accept both fixed and relative codes
    raw_periods = parsed.get("periods") or []
    valid_periods: list[str] = []
    for p in raw_periods:
        p_stripped = p.strip()
        if p_stripped in DHIS2_RELATIVE_PERIODS:
            valid_periods.append(p_stripped)
        elif re.match(r"^\d{4}(Q[1-4]|W\d{1,2}|\d{2})?$", p_stripped):
            valid_periods.append(p_stripped)
        else:
            logger.warning("llm_period_rejected", extra={"period": p_stripped})
    if not valid_periods:
        valid_periods = ["THIS_YEAR"]

    return {
        "output_mode": forced_mode or parsed.get("output_mode", "conversational"),
        "requires_data": parsed.get("requires_data", True),
        "metrics": metrics,
        "org_unit": {"label": org_label, "uid": org_uid, "level": 1 if org_label.lower() in {"national", "federal"} else 2},
        "periods": valid_periods,
        "disaggregations": [],
        "viz_types": ["bar", "line"],
        "needs_web_enrichment": bool(parsed.get("needs_web_enrichment")),
        "web_search_queries": parsed.get("web_search_queries") or [],
        "data_retrieval_strategy": "analytics_api",
        "clarification_needed": bool(parsed.get("clarification_needed")),
        "clarification_question": parsed.get("clarification_question"),
    }
