from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import get_engine
from app.main import create_app


def _bootstrap_app(tmp_path, monkeypatch, name: str = "agent.db"):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / name}")
    monkeypatch.setenv("HELIOS_AGENT_STREAM_DELAY_MS", "0")
    monkeypatch.setenv("HELIOS_AGENT_CONFIG_PATH", str(tmp_path / f"{name}.provider.json"))
    get_settings.cache_clear()
    get_engine.cache_clear()
    return create_app()


def _decode_sse_payloads(response_text: str) -> list[dict]:
    payloads: list[dict] = []
    for line in response_text.splitlines():
        if not line.startswith("data: "):
            continue
        payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


def test_agent_thread_bootstraps_welcome_message_and_setup_profile(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "thread.db")

    with TestClient(app) as client:
        response = client.get("/api/v1/agent/thread")

        assert response.status_code == 200
        payload = response.json()
        assert payload["messages"]
        assert payload["messages"][0]["role"] == "assistant"
        assert "Helios" in payload["messages"][0]["content"]
        assert "summary" in payload["setup_profile"]
        assert payload["pending_proposals"] == []


def test_agent_turn_streams_discovery_activity_and_persists_assistant_reply(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "discovery.db")

    with TestClient(app) as client:
        accepted = client.post("/api/v1/agent/messages", json={"content": "Scan the house and tell me what you find."})
        assert accepted.status_code == 200
        turn_id = accepted.json()["turn_id"]

        stream_response = client.get(f"/api/v1/agent/turns/{turn_id}/events")
        assert stream_response.status_code == 200
        events = _decode_sse_payloads(stream_response.text)

        assert any(event["event_type"] == "tool_started" and event["payload"]["tool_name"] == "refresh_discovery" for event in events)
        assert any(event["event_type"] == "tool_finished" and event["payload"]["tool_name"] == "refresh_discovery" for event in events)
        assert any(event["event_type"] == "assistant_message_completed" for event in events)
        ui_actions_event = next(event for event in events if event["event_type"] == "ui_actions")
        assert any(action["type"] == "open_view" and action["payload"]["view"] == "overview" for action in ui_actions_event["payload"]["actions"])

        thread = client.get("/api/v1/agent/thread").json()
        assert thread["messages"][-1]["role"] == "assistant"
        assert "refreshed discovery" in thread["messages"][-1]["content"].lower()

        overview = client.get("/api/v1/overview").json()
        assert len(overview["devices"]) >= 1


def test_agent_can_propose_and_confirm_system_binding(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "binding.db")

    with TestClient(app) as client:
        discovery_turn = client.post("/api/v1/agent/messages", json={"content": "Scan the house."}).json()["turn_id"]
        client.get(f"/api/v1/agent/turns/{discovery_turn}/events")

        accepted = client.post("/api/v1/agent/messages", json={"content": "I want to integrate my battery."})
        assert accepted.status_code == 200
        turn_id = accepted.json()["turn_id"]
        stream_response = client.get(f"/api/v1/agent/turns/{turn_id}/events")
        events = _decode_sse_payloads(stream_response.text)

        proposal_events = [event for event in events if event["event_type"] == "proposal_created"]
        assert proposal_events
        proposal_id = proposal_events[0]["payload"]["id"]
        assert proposal_events[0]["payload"]["action_type"] == "confirm_system_binding"
        ui_actions_event = next(event for event in events if event["event_type"] == "ui_actions")
        assert any(action["type"] == "focus_system" and action["payload"]["system_type"] == "battery" for action in ui_actions_event["payload"]["actions"])
        assert any(action["type"] == "open_view" and action["payload"]["view"] == "devices" for action in ui_actions_event["payload"]["actions"])

        decision = client.post(f"/api/v1/agent/proposals/{proposal_id}/confirm")
        assert decision.status_code == 200
        setup_profile = client.get("/api/v1/agent/setup-profile").json()
        assert any(binding["system_type"] == "battery" for binding in setup_profile["confirmed_systems"])


def test_agent_can_drive_monitoring_view_for_system_load_question(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "monitoring.db")

    with TestClient(app) as client:
        discovery_turn = client.post("/api/v1/agent/messages", json={"content": "Scan the house."}).json()["turn_id"]
        client.get(f"/api/v1/agent/turns/{discovery_turn}/events")

        accepted = client.post("/api/v1/agent/messages", json={"content": "Do you see load curves for the heat pump?"})
        assert accepted.status_code == 200
        turn_id = accepted.json()["turn_id"]
        stream_response = client.get(f"/api/v1/agent/turns/{turn_id}/events")
        events = _decode_sse_payloads(stream_response.text)

        ui_actions_event = next(event for event in events if event["event_type"] == "ui_actions")
        assert any(action["type"] == "open_view" and action["payload"]["view"] == "monitoring" for action in ui_actions_event["payload"]["actions"])
        assert any(action["type"] == "show_monitoring" for action in ui_actions_event["payload"]["actions"])


def test_agent_provider_config_can_be_updated_without_returning_the_key(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "provider-config.db")

    with TestClient(app) as client:
        initial = client.get("/api/v1/agent/provider-config")
        assert initial.status_code == 200
        assert initial.json()["selected_provider"] == "stub"

        updated = client.patch(
            "/api/v1/agent/provider-config",
            json={
                "provider_id": "openai",
                "model": "gpt-test",
                "base_url": "https://api.openai.example/v1",
                "api_key": "sk-test-secret",
            },
        )
        assert updated.status_code == 200
        payload = updated.json()
        assert payload["selected_provider"] == "openai"
        assert payload["effective_provider"] == "openai"
        assert payload["ready"] is True

        selected_option = next(option for option in payload["provider_options"] if option["provider_id"] == "openai")
        assert selected_option["api_key_configured"] is True
        assert "api_key" not in selected_option


def test_agent_uses_configured_openai_compatible_provider_for_final_response(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "provider-runtime.db")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "I checked the current setup and can help you continue from here.",
                            }
                        ],
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            assert url == "https://api.openai.example/v1/responses"
            assert headers["Authorization"] == "Bearer sk-test-secret"
            assert json["model"] == "gpt-test"
            assert "instructions" in json
            assert "input" in json
            assert "temperature" not in json
            return FakeResponse()

    monkeypatch.setattr("app.agent.provider.httpx.Client", FakeClient)

    with TestClient(app) as client:
        config_response = client.patch(
            "/api/v1/agent/provider-config",
            json={
                "provider_id": "openai",
                "model": "gpt-test",
                "base_url": "https://api.openai.example/v1",
                "api_key": "sk-test-secret",
            },
        )
        assert config_response.status_code == 200

        accepted = client.post("/api/v1/agent/messages", json={"content": "What do you see right now?"})
        assert accepted.status_code == 200
        turn_id = accepted.json()["turn_id"]

        stream_response = client.get(f"/api/v1/agent/turns/{turn_id}/events")
        assert stream_response.status_code == 200
        events = _decode_sse_payloads(stream_response.text)
        completed = next(event for event in events if event["event_type"] == "assistant_message_completed")
        assert "I checked the current setup" in completed["payload"]["message"]["content"]
        assert completed["payload"]["ui_actions"]
