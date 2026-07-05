"""Agent stream tests — all output modes + clarification."""
import pytest
from unittest.mock import AsyncMock, patch

from backend.config import Settings
from backend.app.agent.graph import run_agent_stream
from backend.app.models import ChatRequest, Identity


def _settings():
    return Settings(
        llm_provider="openai",
        llm_api_key="fake-key",
        embedding_provider="mock",
        dhis2_service_account_user="",
        dhis2_service_account_pass="",
    )


def _identity():
    return Identity(user_id="test_user", role="dhis2_user")


@pytest.fixture(autouse=True)
def mock_llm_complete():
    with patch("backend.app.llm.complete", new_callable=AsyncMock) as mock:
        async def side_effect(messages, settings, json_mode=False, temperature=0.3):
            if json_mode:
                user_msg = messages[-1]["content"]
                output_mode = "conversational"
                if "dashboard" in user_msg.lower() or "trends" in user_msg.lower():
                    output_mode = "dashboard"
                elif "report" in user_msg.lower() or "monthly" in user_msg.lower():
                    output_mode = "report"
                elif "presentation" in user_msg.lower() or "briefing" in user_msg.lower():
                    output_mode = "presentation"
                elif "export" in user_msg.lower() or "excel" in user_msg.lower():
                    output_mode = "export"
                return f"""{{
                    "output_mode": "{output_mode}",
                    "requires_data": true,
                    "metrics": [{{"label": "Malaria Confirmed Cases", "uid": null, "object_type": "indicator"}}],
                    "org_unit_label": "Kaduna",
                    "periods": ["2024"],
                    "needs_web_enrichment": false,
                    "web_search_queries": [],
                    "clarification_needed": false,
                    "clarification_question": null
                }}"""
            else:
                if "HTML" in messages[-1]["content"]:
                    return "<h1>Mock Report</h1><p>This is a mock report.</p>"
                return "This is a mock conversational response from the LLM."
        mock.side_effect = side_effect
        yield mock


@pytest.mark.asyncio
async def test_dashboard_stream():
    request = ChatRequest(message="Show me malaria trends in Kaduna", output_mode="dashboard")
    events = []
    async for item in run_agent_stream(request, _settings(), _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: chart_config" in joined
    assert "event: done" in joined


@pytest.mark.asyncio
async def test_conversational_stream():
    request = ChatRequest(message="Tell me about ANC coverage in Nigeria")
    events = []
    async for item in run_agent_stream(request, _settings(), _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: token" in joined
    assert "event: done" in joined


@pytest.mark.asyncio
async def test_report_stream():
    request = ChatRequest(message="Prepare the monthly programme review for malaria", output_mode="report")
    events = []
    async for item in run_agent_stream(request, _settings(), _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: report_html" in joined
    assert "event: done" in joined


@pytest.mark.asyncio
async def test_presentation_stream():
    request = ChatRequest(message="Create a briefing deck for OPV3", output_mode="presentation")
    events = []
    async for item in run_agent_stream(request, _settings(), _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: slide_manifest" in joined
    assert "event: done" in joined


@pytest.mark.asyncio
async def test_export_stream():
    request = ChatRequest(message="Give me raw numbers in Excel for cholera", output_mode="export")
    events = []
    async for item in run_agent_stream(request, _settings(), _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: data_ready" in joined
    assert "event: done" in joined


@pytest.mark.asyncio
async def test_evidence_emitted_when_fusion_enabled():
    settings = _settings()
    settings.evidence_fusion = True
    request = ChatRequest(message="Show me malaria trends in Kaduna", output_mode="dashboard")
    events = []
    async for item in run_agent_stream(request, settings, _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: evidence" in joined


@pytest.mark.asyncio
async def test_no_evidence_when_fusion_disabled():
    settings = _settings()
    settings.evidence_fusion = False
    request = ChatRequest(message="Show me malaria trends", output_mode="dashboard")
    events = []
    async for item in run_agent_stream(request, settings, _identity()):
        events.append(item)
    joined = "".join(events)
    assert "event: evidence" not in joined
