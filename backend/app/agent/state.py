from typing import Literal, Optional, TypedDict


class MetricRef(TypedDict):
    label: str
    uid: str
    uid_confidence: float
    object_type: str


class OrgUnitRef(TypedDict):
    label: str
    uid: str
    level: int


class EvidenceItem(TypedDict):
    claim: str
    source: Literal["dhis2", "tavily", "llm"]
    source_detail: str
    confidence: float


class AgentState(TypedDict):
    messages: list[dict]
    session_id: str
    user_id: str
    user_role: Literal["dhis2_user", "external_stakeholder"]

    output_mode: Literal["conversational", "dashboard", "report", "presentation", "export", "map"]
    metrics: list[MetricRef]
    org_unit: OrgUnitRef
    periods: list[str]
    disaggregations: list[str]
    viz_types: list[str]
    needs_web_enrichment: bool
    web_search_queries: list[str]
    data_retrieval_strategy: Literal["analytics_api", "direct_sql", "both", "tracker_api", "data_value_sets"]
    clarification_needed: bool
    clarification_question: Optional[str]
    requires_data: bool

    dhis2_data: dict
    pii_map: dict[str, str]
    web_context: list[dict]
    evidence_items: list[EvidenceItem]

    active_report_html: str
    active_chart_configs: list[dict]
    active_slide_manifest: list[dict]
    generated_file_id: Optional[str]
    active_conversational_response: Optional[str]
    fetch_error: Optional[str]
    # Raw DHIS2 personal access token for the current user, when authenticated
    # in dhis2/combined mode; enables per-user DHIS2 API calls instead of mock.
    _dhis2_token: Optional[str]
