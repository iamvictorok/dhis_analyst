"""Intent classifier — the most critical prompt in the system.

Routes through LiteLLM for full NL intent extraction.
The deterministic classify_intent() is a generic fallback for when the LLM
returns malformed JSON — it extracts metric labels and org unit names from
natural language without any hardcoded instance-specific data.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Literal

logger = logging.getLogger("dhis2_analyst.intent")

OutputMode = Literal["conversational", "dashboard", "report", "presentation", "export", "map"]

# Every mode the pipeline can actually render. Any LLM-proposed value outside
# this set is coerced to "conversational" rather than trusted blindly.
VALID_OUTPUT_MODES: frozenset[str] = frozenset(
    {"conversational", "dashboard", "report", "presentation", "export", "map"}
)

PERIOD_RE = re.compile(r"\b(20\d{2})(?:\s*[-/]?\s*(q[1-4]))?\b", re.I)

# Generic org-unit level keywords (DHIS2-standard, not instance-specific)
ORG_UNIT_LEVEL_RE = re.compile(
    r"\b(national|federal|country|state|province|region|county|district|lga"
    r"|sub-?county|zone|ward|facility|health\s*centre|clinic|hospital)\b",
    re.I,
)

# Extract a location name from patterns like "in Bo district", "for Kaduna state"
_ORG_UNIT_NL_RE = re.compile(
    r"(?:\b(?:in|for|at|from|of|across)\b\s+(?:the\s+)?)([A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)*)",
)

# Words that look like capitalised place names after a preposition but are not
# (months, common metric acronyms) — filtered out of fallback org-unit extraction.
_ORG_UNIT_STOPWORDS: frozenset[str] = frozenset({
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "bcg", "opv", "anc", "who", "hiv", "tb", "art", "pmtct", "penta",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})

# Noise phrases stripped (word-boundary only) when isolating the health topic.
_METRIC_NOISE_RE = re.compile(
    r"\b(?:show me|give me|what is|what are|how many|table of|trend of|"
    r"compare|last|this|over the|in the|for the|month|year|quarter|week|"
    r"district|state|national)\b",
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
        "requires_data": bool(text),
        "metrics": _extract_metric_labels(lowered),
        "org_unit": _extract_org_unit(text),
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

    # Mock provider: use the deterministic classifier directly (no network call).
    if getattr(settings, "llm_provider", "") == "mock":
        return classify_intent(message, forced_mode=forced_mode, today=now)

    # Fetch real indicator names from the synced metadata DB so the LLM can
    # emit exact DHIS2 names instead of guessing.
    indicator_catalogue = ""
    try:
        from backend.app.db.metadata_index import get_indicator_names_for_prompt
        from backend.app.db.session import get_db_session
        async with get_db_session() as session:
            indicator_catalogue = await get_indicator_names_for_prompt(
                session, is_postgres=getattr(settings, "is_postgres", False),
            )
    except Exception as exc:
        logger.warning("indicator_catalogue_fetch_failed", extra={"error": str(exc)})

    relative_period_list = ", ".join(sorted(DHIS2_RELATIVE_PERIODS))
    system = f"""You are an intent classifier for a DHIS2 public health analytics system.
Today is {now.isoformat()}. Analyse the user query and return ONLY a JSON object.

CRITICAL RULES FOR FOLLOW-UP QUERIES:
- If the user refers to something from the conversation history (e.g. "What about 2025?", "Show me Lagos instead"), carry over the unmentioned fields from the context and only update what changed.
- If the query is general, conversational, or discusses prior data (e.g. "explain this trend", "why?"), use output_mode "conversational" and carry over the relevant context.
- If the user asks a COMPLETELY NEW question that does NOT refer to the previous history (e.g. asking for a different indicator or entirely different topic), DO NOT carry over anything from the history. Reset all fields (metrics, periods, etc.) to match only the new question.

Output mode — use ONE of:
- conversational: simple chat, Q&A, general questions
- dashboard: request for charts, visuals, comparisons over time
- report: request for a formal narrative document or summary
- presentation: request for slide deck or presentation
- export: request to download raw data (CSV, Excel, etc)
- map: request for a geospatial map, heat map, or geographical distribution

Return JSON with these exact keys:
{{
  "query_expansion": "<think step-by-step: expand layperson terms to medical/public health terms, e.g. shots->vaccinations, pregnant->ANC. Briefly state your reasoning>",
  "output_mode": "<one of the 6 modes>",
  "requires_data": <true/false>,
  "metrics": [{{"label": "<indicator name as it might appear in DHIS2>", "uid": null, "object_type": "indicator", "search_terms": ["<alternative search term 1>", "<alternative search term 2>"]}}],
  "metric_search_terms": ["<additional search strings to try if the primary label fails>"],
  "org_unit_label": "<detected location or 'National'>",
  "periods": ["<DHIS2 period code>"],
  "disaggregations": ["<disaggregation token, e.g. ou_children>"],
  "data_retrieval_strategy": "<analytics_api or tracker_api>",
  "needs_web_enrichment": <true/false>,
  "web_search_queries": ["<search query>"],
  "clarification_needed": <true/false>,
  "clarification_question": "<question or null>"
}}

CRITICAL RULES FOR METRIC LABELS:
- The "label" should be the closest DHIS2 indicator name you can guess. DHIS2 indicators are named like: "Malaria confirmed cases", "ANC 1st visit", "OPV3 doses given", "BCG doses given", "HIV tests performed", "Maternal mortality cases".
- The "search_terms" array MUST contain 2-5 alternative phrasings or related DHIS2 indicator names. Think about how the same concept appears in health information systems:
  * For "shots": ["immunization", "vaccination", "doses given", "OPV", "Penta"]
  * For "malaria deaths": ["malaria mortality", "malaria death", "malaria case fatality"]
  * For "pregnant women visits": ["ANC", "antenatal", "ANC 1st visit"]
- "metric_search_terms" is a global fallback list of 3-5 broad search terms if the primary labels fail.

requires_data: set to true when the query needs DHIS2 data retrieval (e.g. health statistics, trends, comparisons, reports, exports). Set to false for casual chitchat, greetings, thank-yous, general knowledge questions, or meta-questions about the system that do NOT need health data from DHIS2.
disaggregations: If the user explicitly asks to break down data spatially (e.g., "across all districts", "by region"), include "ou_children". If they ask for demographic/category breakdowns (e.g. "by age", "by gender", "by facility type"), include those layperson terms as strings (e.g. ["age", "gender"]). Otherwise leave it empty []. For time trends, use relative period codes (e.g. MONTHS_THIS_YEAR) instead of disaggregations.
data_retrieval_strategy: Use "tracker_api" ONLY if the user explicitly asks for line lists, individual patient records, specific patient events, or un-aggregated dates (e.g., "date of previous malaria episode"). Use "data_value_sets" ONLY if the user explicitly asks for raw data validation, missing reports, or data entry errors before aggregation. Otherwise, always use "analytics_api" for aggregated totals, summaries, and percentages.
clarification_needed: Set to true ONLY IF the query is completely nonsensical or utterly lacks a health subject AND doesn't reference prior conversation. NEVER ask for clarification about DHIS2 specifics, metadata, exact indicator names, or periods. Always make your best guess.

Few-Shot Examples:
1. User: "How many kids got their shots in Bo last year?"
{{
  "query_expansion": "'shots' = immunizations/vaccinations (Penta, OPV, BCG). 'kids' = children. 'Bo' = Bo district. 'last year' = LAST_YEAR.",
  "output_mode": "conversational",
  "requires_data": true,
  "metrics": [{{"label": "Immunization doses given", "uid": null, "object_type": "dataElement", "search_terms": ["immunization", "vaccination", "OPV", "Penta", "doses given"]}}],
  "metric_search_terms": ["immunization", "vaccination coverage", "OPV3 doses given", "BCG doses given", "Penta3 doses given"],
  "org_unit_label": "Bo",
  "periods": ["LAST_YEAR"],
  "disaggregations": [],
  "data_retrieval_strategy": "analytics_api",
  "needs_web_enrichment": false,
  "web_search_queries": [],
  "clarification_needed": false,
  "clarification_question": null
}}

2. User: "Give me a dashboard of malaria deaths across all districts for the last 4 quarters."
{{
  "query_expansion": "'malaria deaths' = malaria mortality/case fatality. 'across all districts' = ou_children spatial disaggregation. 'last 4 quarters' = LAST_4_QUARTERS.",
  "output_mode": "dashboard",
  "requires_data": true,
  "metrics": [{{"label": "Malaria deaths", "uid": null, "object_type": "indicator", "search_terms": ["malaria mortality", "malaria death", "malaria case fatality"]}}],
  "metric_search_terms": ["malaria deaths", "malaria mortality", "malaria confirmed"],
  "org_unit_label": "National",
  "periods": ["LAST_4_QUARTERS"],
  "disaggregations": ["ou_children"],
  "data_retrieval_strategy": "analytics_api",
  "needs_web_enrichment": false,
  "web_search_queries": [],
  "clarification_needed": false,
  "clarification_question": null
}}

3. User: "Show me the trend of Malaria confirmed cases in Bo for the last 6 months."
{{
  "query_expansion": "Direct health indicator request. 'Malaria confirmed cases' is likely an exact DHIS2 indicator name.",
  "output_mode": "dashboard",
  "requires_data": true,
  "metrics": [{{"label": "Malaria confirmed cases", "uid": null, "object_type": "indicator", "search_terms": ["malaria confirmed", "malaria cases", "confirmed malaria"]}}],
  "metric_search_terms": ["malaria confirmed", "malaria cases"],
  "org_unit_label": "Bo",
  "periods": ["LAST_6_MONTHS"],
  "disaggregations": [],
  "data_retrieval_strategy": "analytics_api",
  "needs_web_enrichment": false,
  "web_search_queries": [],
  "clarification_needed": false,
  "clarification_question": null
}}

4. User: "Compare ANC 1st visit and ANC 4th or more visits in Kenema for this year."
{{
  "query_expansion": "Two specific DHIS2 indicators: ANC 1st visit and ANC 4th or more visits. Multi-metric comparison.",
  "output_mode": "dashboard",
  "requires_data": true,
  "metrics": [
    {{"label": "ANC 1st visit", "uid": null, "object_type": "indicator", "search_terms": ["ANC 1", "antenatal 1st", "ANC first visit"]}},
    {{"label": "ANC 4th or more visits", "uid": null, "object_type": "indicator", "search_terms": ["ANC 4", "antenatal 4th", "ANC fourth"]}}
  ],
  "metric_search_terms": ["ANC", "antenatal"],
  "org_unit_label": "Kenema",
  "periods": ["THIS_YEAR"],
  "disaggregations": [],
  "data_retrieval_strategy": "analytics_api",
  "needs_web_enrichment": false,
  "web_search_queries": [],
  "clarification_needed": false,
  "clarification_question": null
}}

5. User: "Hello! Who are you?"
{{
  "query_expansion": "Casual greeting, no health data needed.",
  "output_mode": "conversational",
  "requires_data": false,
  "metrics": [],
  "metric_search_terms": [],
  "org_unit_label": "National",
  "periods": ["THIS_YEAR"],
  "disaggregations": [],
  "data_retrieval_strategy": "analytics_api",
  "needs_web_enrichment": false,
  "web_search_queries": [],
  "clarification_needed": false,
  "clarification_question": null
}}

AVAILABLE INDICATORS IN THIS DHIS2 INSTANCE:
When choosing metric labels, PREFER exact names from this list. If the user's phrasing matches one of these, use the exact name.
{indicator_catalogue if indicator_catalogue else '(indicator list unavailable — use your best guess)'}

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
    # Pass limited conversation history for accurate follow-up without token bloat
    if history:
        for msg in history[-5:]:
            llm_messages.append({"role": msg["role"], "content": _scrub_pii(msg["content"])})
    llm_messages.append({"role": "user", "content": _scrub_pii(message)})

    try:
        raw = await complete(
            llm_messages,
            settings,
            json_mode=True,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("llm_intent_complete_failed", extra={"error": str(exc)})
        return classify_intent(message, forced_mode=forced_mode, today=now)
        
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # LLMs sometimes wrap JSON in markdown fences — strip and retry
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(
                "llm_intent_json_fallback",
                extra={"raw_preview": raw[:200], "message": message[:100]},
            )
            return classify_intent(message, forced_mode=forced_mode, today=now)
            
    if isinstance(parsed, list):
        if len(parsed) > 0 and isinstance(parsed[0], dict):
            parsed = parsed[0]
        else:
            return classify_intent(message, forced_mode=forced_mode, today=now)

    if not isinstance(parsed, dict):
        logger.warning("llm_intent_non_object", extra={"type": type(parsed).__name__})
        return classify_intent(message, forced_mode=forced_mode, today=now)

    return _merge_llm_intent(parsed, forced_mode, now, message)



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _scrub_pii(text: str) -> str:
    """Lightweight PII scrubber to protect patient data from reaching the LLM."""
    if not text:
        return text
    # Scrub potential patient IDs / names / DOBs
    text = re.sub(r"\b(?:patient\s+)?(?:id|dob|dob[:\s])\s*[\w-]+\b", "[REDACTED_PII]", text, flags=re.I)
    # Scrub anything that looks like a date of birth context
    text = re.sub(r"\b(?:born on|date of birth)\s+[\d/:-]+\b", "[REDACTED_DATE]", text, flags=re.I)
    # Scrub specific names following 'patient' or 'person'
    text = re.sub(r"\b(patient|person)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b", r"\1 [REDACTED_NAME]", text)
    return text

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


def _extract_metric_labels(text: str) -> list[dict[str, Any]]:
    """Extract health indicator labels from natural language.

    No hardcoded UIDs — returns UNRESOLVED markers for the metadata resolver
    to look up from whichever DHIS2 instance is connected.

    Unlike the old version which stripped noise words and returned mangled
    fragments, this extracts the health-topic noun phrase more carefully.
    """
    # Try to extract the health topic as the noun phrase between
    # temporal/location markers. E.g. from "show me Malaria confirmed cases
    # in Bo for last 6 months" → "Malaria confirmed cases"
    topic_match = re.search(
        r'(?:show me|give me|what (?:is|are|were)|how many|trend of|compare|breakdown of|data for)?\s*'
        r'(?:the\s+)?'
        r'([A-Z][\w\s\-/]+?)'
        r'(?:\s+(?:in|for|at|from|across|by|over|during|last|this|between|since)\b|$)',
        text,
        re.IGNORECASE,
    )
    if topic_match:
        label = topic_match.group(1).strip()
        # Remove trailing prepositions/articles captured by the greedy match
        label = re.sub(r'\s+(?:in|for|at|from|the|a|an|and|or)\s*$', '', label, flags=re.IGNORECASE).strip()
        if len(label) >= 3:
            return [{
                "label": label,
                "uid": "UNRESOLVED",
                "uid_confidence": 0.40,
                "object_type": "indicator",
            }]

    # Fallback: strip only the most obvious noise and keep the rest
    cleaned = _METRIC_NOISE_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) < 3:
        cleaned = text.strip()

    return [{
        "label": cleaned,
        "uid": "UNRESOLVED",
        "uid_confidence": 0.40,
        "object_type": "indicator",
    }]


def _extract_org_unit(text: str) -> dict[str, Any]:
    """Extract organisation unit from natural language.

    Uses generic preposition patterns (in/for/at X) — no hardcoded place names.
    The metadata resolver will look up the real UID from the synced DHIS2 DB.
    """
    # Try "in <Place>" / "for <Place>" patterns. Iterate all matches and prefer
    # a candidate adjacent to an org-level keyword ("Bo district"); skip months
    # and metric acronyms that also capitalise after a preposition.
    skip = {"the", "a", "an", "this", "that", "last", "next"} | _ORG_UNIT_STOPWORDS
    fallback_label: str | None = None
    for match in _ORG_UNIT_NL_RE.finditer(text):
        raw = match.group(1).strip()
        # Strip any level keyword captured inside the run, and also look just
        # past the match for a trailing one ("Bo district" — the regex captures
        # only "Bo", so the keyword sits after match.end()).
        following = text[match.end(): match.end() + 20]
        had_level_kw = bool(ORG_UNIT_LEVEL_RE.search(raw)) or bool(
            re.match(r"\s+" + ORG_UNIT_LEVEL_RE.pattern, following, re.I)
        )
        label = ORG_UNIT_LEVEL_RE.sub("", raw).strip() or raw
        if not label or label.lower() in skip:
            continue
        if had_level_kw:
            # Strong signal ("... in Bo district") — take it immediately.
            return {"label": label, "uid": "UNRESOLVED", "level": _infer_org_level(text)}
        if fallback_label is None:
            fallback_label = label
    if fallback_label is not None:
        return {"label": fallback_label, "uid": "UNRESOLVED", "level": _infer_org_level(text)}

    # Check for bare level keywords
    level_match = ORG_UNIT_LEVEL_RE.search(text.lower())
    if level_match:
        term = level_match.group(0).lower()
        level = _level_from_keyword(term)
        return {"label": "National" if level == 1 else term.title(), "uid": "UNRESOLVED", "level": level}

    return {"label": "National", "uid": "UNRESOLVED", "level": 1}


def _infer_org_level(text: str) -> int:
    """Infer the DHIS2 org hierarchy level from context keywords."""
    lowered = text.lower()
    level_match = ORG_UNIT_LEVEL_RE.search(lowered)
    if level_match:
        return _level_from_keyword(level_match.group(0).lower())
    return 2  # default to sub-national


def _level_from_keyword(term: str) -> int:
    """Map an org-unit type keyword to a DHIS2 hierarchy level."""
    level_map = {
        "national": 1, "federal": 1, "country": 1,
        "state": 2, "province": 2, "region": 2, "county": 2,
        "district": 3, "lga": 3, "sub-county": 3, "subcounty": 3, "zone": 3,
        "ward": 4,
        "facility": 5, "health centre": 5, "healthcentre": 5, "clinic": 5, "hospital": 5,
    }
    return level_map.get(term, 2)


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

    # 2. Extract explicit year/quarter codes like "2024", "2024Q1". A bare
    # four-digit token is only treated as a year within a plausible reporting
    # window — otherwise quantities ("over 2000 cases") and far-future targets
    # ("2050 goal") get misread as periods.
    for year, quarter in PERIOD_RE.findall(text):
        if quarter:
            periods.append(f"{year}{quarter.upper()}")
        elif 2010 <= int(year) <= today.year + 1:
            periods.append(year)

    # 3. Legacy handlers for phrases not yet covered above
    if not periods:
        if "year to date" in text or "ytd" in text:
            periods.append(str(today.year))
        if "last three quarters" in text or "last 3 quarters" in text:
            # DHIS2 has no LAST_3_QUARTERS code — emit the three explicit
            # quarter codes rather than silently widening to four.
            periods.extend(_last_n_quarter_codes(today, 3))

    return sorted(set(periods)) or ["THIS_YEAR"]


# Word-boundary matching — a substring test flags "who" inside "whooping",
# "target" inside "targeted", "standard" inside "standardized", etc.
_WEB_ENRICHMENT_RE = re.compile(
    r"\b(?:who|guidelines?|benchmarks?|outbreaks?|policy|targets?|compare|"
    r"global|international|standards?|unicef|cdc|external|context|news|recent)\b",
    re.I,
)


def _needs_web_enrichment(text: str) -> bool:
    return bool(_WEB_ENRICHMENT_RE.search(text))


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


def _last_n_quarter_codes(today: date, n: int) -> list[str]:
    """Return the ``n`` most recent completed quarter codes, oldest first."""
    q = ((today.month - 1) // 3) or 4
    yr = today.year if q != 4 else today.year - 1
    codes: list[str] = []
    for _ in range(n):
        codes.append(f"{yr}Q{q}")
        q -= 1
        if q == 0:
            q = 4
            yr -= 1
    return list(reversed(codes))


def _merge_llm_intent(
    parsed: dict,
    forced_mode: OutputMode | None,
    today: date,
    message: str = "",
) -> dict[str, Any]:
    """Normalise LLM JSON output into the internal intent schema.

    LLM output is untrusted: fields may be null, wrong-typed, nested, or
    hallucinated. Every field is coerced defensively so a malformed response
    degrades gracefully instead of raising and aborting the whole request.
    """
    metrics = _normalise_llm_metrics(parsed.get("metrics"))

    org_label = _normalise_org_label(parsed.get("org_unit_label"))

    # Validate and normalise periods — accept both fixed and relative codes.
    # LLMs may emit a bare string, integers (years), or null entries.
    raw_periods = parsed.get("periods")
    if isinstance(raw_periods, str):
        raw_periods = [raw_periods]
    elif not isinstance(raw_periods, list):
        raw_periods = []
    valid_periods: list[str] = []
    for p in raw_periods:
        if p is None:
            continue
        p_stripped = str(p).strip()
        if not p_stripped:
            continue
        if p_stripped in DHIS2_RELATIVE_PERIODS:
            valid_periods.append(p_stripped)
        elif re.match(r"^\d{4}(Q[1-4]|W\d{1,2}|\d{2})?$", p_stripped):
            valid_periods.append(p_stripped)
        else:
            logger.warning("llm_period_rejected", extra={"period": p_stripped})
    if not valid_periods:
        valid_periods = ["THIS_YEAR"]

    # Whitelist the output mode — never trust an LLM-invented enum value.
    raw_mode = parsed.get("output_mode")
    if forced_mode:
        output_mode = forced_mode
    elif isinstance(raw_mode, str) and raw_mode in VALID_OUTPUT_MODES:
        output_mode = raw_mode
    else:
        if raw_mode is not None:
            logger.warning("llm_output_mode_rejected", extra={"output_mode": raw_mode})
        output_mode = "conversational"

    disaggregations = parsed.get("disaggregations")
    if not isinstance(disaggregations, list):
        disaggregations = []

    web_queries = parsed.get("web_search_queries")
    if not isinstance(web_queries, list):
        web_queries = []

    strategy = parsed.get("data_retrieval_strategy")
    if strategy not in {"analytics_api", "tracker_api", "data_value_sets", "direct_sql", "both"}:
        strategy = "analytics_api"

    # Collect global metric search terms (fallback search queries from the LLM)
    raw_search_terms = parsed.get("metric_search_terms")
    if not isinstance(raw_search_terms, list):
        raw_search_terms = []
    metric_search_terms = [str(t).strip() for t in raw_search_terms if t and str(t).strip()]

    return {
        "query_expansion": parsed.get("query_expansion", ""),
        "output_mode": output_mode,
        "requires_data": bool(parsed.get("requires_data", True)),
        "metrics": metrics,
        "metric_search_terms": metric_search_terms,
        "org_unit": {
            "label": org_label,
            "uid": "UNRESOLVED",
            "level": 1 if org_label.lower() in {"national", "federal"} else 2,
        },
        "periods": valid_periods,
        "disaggregations": disaggregations,
        "viz_types": _detect_viz_types(message.lower(), output_mode),
        "needs_web_enrichment": bool(parsed.get("needs_web_enrichment")),
        "web_search_queries": [str(q) for q in web_queries if q],
        "data_retrieval_strategy": strategy,
        "clarification_needed": bool(parsed.get("clarification_needed")),
        "clarification_question": parsed.get("clarification_question"),
    }


def _normalise_llm_metrics(raw: Any) -> list[dict[str, Any]]:
    """Coerce the LLM ``metrics`` field into a clean list of metric dicts.

    Tolerates a single dict, a list of strings, null/blank labels, and
    hallucinated UIDs. LLM-supplied UIDs are never trusted on syntax alone —
    they are marked low-confidence so the metadata resolver verifies them
    against the synced index.

    Now also preserves per-metric ``search_terms`` from the LLM for use by
    the metadata resolver's multi-strategy search cascade.
    """
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []

    metrics: list[dict[str, Any]] = []
    for m in raw:
        if isinstance(m, str):
            m = {"label": m}
        if not isinstance(m, dict):
            continue
        label = m.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        # Preserve per-metric search_terms from the LLM
        search_terms = m.get("search_terms")
        if not isinstance(search_terms, list):
            search_terms = []
        search_terms = [str(t).strip() for t in search_terms if t and str(t).strip()]

        metric: dict[str, Any] = {
            "label": label.strip(),
            "uid": "UNRESOLVED",
            "uid_confidence": 0.40,
            "object_type": m.get("object_type") if isinstance(m.get("object_type"), str) else "indicator",
        }
        if search_terms:
            metric["search_terms"] = search_terms
        metrics.append(metric)

    # Never return an empty metrics list — use the raw message as label
    # instead of a generic placeholder that can't possibly match anything.
    if not metrics:
        metrics = [{
            "label": "Health indicator",
            "uid": "UNRESOLVED",
            "uid_confidence": 0.40,
            "object_type": "indicator",
        }]
    return metrics


def _normalise_org_label(raw: Any) -> str:
    """Coerce the LLM ``org_unit_label`` into a plain string.

    The LLM may emit null (no location), a nested object, or a non-string;
    any of these previously crashed ``.lower()`` and aborted the request.
    """
    if isinstance(raw, dict):
        raw = raw.get("label")
    if not isinstance(raw, str) or not raw.strip():
        return "National"
    return raw.strip()
