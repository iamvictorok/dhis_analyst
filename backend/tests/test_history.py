import pytest
from unittest.mock import AsyncMock, patch
from datetime import date
from backend.config import Settings
from backend.app.agent.intent import classify_intent_llm
from backend.app.agent.graph import build_initial_state, run_agent_stream
from backend.app.models import ChatRequest, Identity


@pytest.mark.asyncio
async def test_classify_intent_llm_passes_history():
    settings = Settings(
        llm_provider="openai",
        llm_model="gpt-4o",
        jwt_secret="test",
        llm_api_key="sk-test-api-key"
    )
    mock_response = """{
        "output_mode": "conversational",
        "metrics": [{"label": "Malaria Confirmed Cases", "uid": null, "object_type": "indicator"}],
        "org_unit_label": "Lagos",
        "periods": ["2025"],
        "needs_web_enrichment": false,
        "web_search_queries": [],
        "clarification_needed": false,
        "clarification_question": null
    }"""
    
    with patch("backend.app.llm.complete", new_callable=AsyncMock) as mock_complete:
        mock_complete.return_value = mock_response
        
        history = [
            {"role": "user", "content": "Show malaria cases in Kaduna"},
            {"role": "assistant", "content": "Here is the data for malaria in Kaduna."}
        ]
        
        result = await classify_intent_llm(
            message="What about Lagos in 2025?",
            settings=settings,
            today=date(2026, 6, 13),
            history=history
        )
        
        # Assert complete was called
        mock_complete.assert_called_once()
        call_args = mock_complete.call_args[0]
        messages_passed = call_args[0]
        
        # Check message ordering: system, history user, history assistant, current user
        assert len(messages_passed) == 4
        assert messages_passed[0]["role"] == "system"
        assert messages_passed[1]["role"] == "user"
        assert messages_passed[1]["content"] == "Show malaria cases in Kaduna"
        assert messages_passed[2]["role"] == "assistant"
        assert messages_passed[2]["content"] == "Here is the data for malaria in Kaduna."
        assert messages_passed[3]["role"] == "user"
        assert messages_passed[3]["content"] == "What about Lagos in 2025?"
        
        # Check result properties
        assert result["output_mode"] == "conversational"
        assert result["periods"] == ["2025"]


@pytest.mark.asyncio
async def test_build_initial_state_contains_history():
    settings = Settings(
        llm_provider="openai",
        llm_api_key="fake-key",
        embedding_provider="mock",
    )
    request = ChatRequest(message="What about Oyo?", conversation_id="conv123")
    identity = Identity(user_id="user1", role="external_stakeholder")
    history = [
        {"role": "user", "content": "Show malaria in Lagos"},
        {"role": "assistant", "content": "Here is malaria in Lagos."}
    ]
    
    mock_response = """{
        "output_mode": "conversational",
        "metrics": [{"label": "Malaria Confirmed Cases", "uid": null, "object_type": "indicator"}],
        "org_unit_label": "Oyo",
        "periods": ["2025"],
        "needs_web_enrichment": false,
        "web_search_queries": [],
        "clarification_needed": false,
        "clarification_question": null
    }"""
    
    with patch("backend.app.llm.complete", new_callable=AsyncMock) as mock_complete:
        mock_complete.return_value = mock_response
        state = await build_initial_state(request, settings, identity, history=history)
    
    # State messages should contain history + current query
    assert len(state["messages"]) == 3
    assert state["messages"][0]["content"] == "Show malaria in Lagos"
    assert state["messages"][1]["content"] == "Here is malaria in Lagos."
    assert state["messages"][2]["content"] == "What about Oyo?"
    assert state["active_conversational_response"] is None
