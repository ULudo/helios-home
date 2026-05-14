from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import (
    AgentTask,
    AuditEvent,
    Blocker,
    Device,
    DeviceCandidate,
    HemsSystemBinding,
    HomeGraphEntity,
    ProtocolDiagnosticRun,
    ProtocolEndpoint,
    Proposal,
    TaskStep,
    ToolInvocation,
    UserDecisionRequest,
    Site,
    SiteSetupProfile,
    utcnow,
)
from app.db.session import get_engine, get_session_factory
from app.agent.configuration import PROVIDER_SPECS, ProviderRuntimeStatus, ProviderState
from app.home_graph.service import sync_inventory_to_home_graph
from app.main import create_app
from app.agent.provider import (
    LLMModelProvider,
    ModelFinalAnswer,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    _model_action_from_openai_responses_payload,
    _provider_tool_name_map,
)
from app.agent.tools.registry import create_default_tool_registry
from app.services.eebus_runtime import EebusPeerTrustMaterial, EebusRuntimeSnapshot
from app.services.http_telemetry import HttpTelemetryProbeResult
from app.services.modbus import ModbusProbeResult


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
        if line.startswith("data: "):
            payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


@dataclass(slots=True)
class ScriptedModelProvider:
    actions: list[ModelToolCall | ModelFinalAnswer]
    provider_name: str = "scripted"
    requests: list[ModelRequest] = field(default_factory=list)

    def next_action(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.actions:
            raise AssertionError("Scripted model provider ran out of actions.")
        action = self.actions.pop(0)
        if request.force_final and isinstance(action, ModelToolCall):
            raise AssertionError("Scripted model returned a tool call when force_final=True.")
        return ModelResponse(
            action=action,
            provider_name=self.provider_name,
            model="scripted-model",
            raw_text="<scripted>",
            latency_ms=1,
            token_usage={"scripted": True},
        )


def _use_scripted_provider(monkeypatch, provider: ScriptedModelProvider) -> ScriptedModelProvider:
    monkeypatch.setattr("app.agent.service.get_model_provider", lambda runtime: provider)
    return provider


def _site_id() -> int:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.scalar(select(Site).limit(1))
        assert site is not None
        return site.id


def _add_device(
    *,
    device_id: str,
    name: str,
    device_type: str,
    manufacturer: str = "",
    model: str = "",
    protocols: list[str] | None = None,
) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.get(Site, _site_id())
        assert site is not None
        device = Device(
            id=device_id,
            site_id=site.id,
            name=name,
            manufacturer=manufacturer,
            model=model,
            firmware="unknown",
            device_type=device_type,
            primary_status="visible_only",
            status_tags=["discovered", "visible_only"],
            confidence=0.84,
            recovery_zone="human_gated",
            protocols=protocols or ["eebus_ship", "mdns"],
            capabilities={
                "visible": True,
                "monitorable": False,
                "controllable": False,
                "optimizable": False,
            },
            telemetry={"eebus_ship_advertised": True, "ship_port": 4711},
        )
        session.add(device)
        session.commit()


def _add_wallbox_candidate(device_id: str, candidate_id: str, display_name: str) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.get(Site, _site_id())
        assert site is not None
        candidate = DeviceCandidate(
            id=candidate_id,
            site_id=site.id,
            stable_key=device_id,
            display_name=display_name,
            manufacturer="MENNEKES",
            model=display_name,
            firmware="unknown",
            device_type="wallbox",
            discovery_sources=["eebus_ship_live"],
            protocols=["eebus_ship", "mdns"],
            evidence={"host": "192.168.188.186"},
            classification_confidence=0.84,
            classification_reasoning="EEBus SHIP advertisement matched wallbox identity.",
            state="classified",
            matched_device_id=device_id,
        )
        session.add(candidate)
        session.commit()


def _add_ppc_eebus_candidate(device_id: str = "dev-ppc", *, register: bool = False) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.get(Site, _site_id())
        assert site is not None
        candidate = DeviceCandidate(
            id="cand-ppc-eebus",
            site_id=site.id,
            stable_key=device_id,
            display_name="Steuereinrichtung",
            manufacturer="PPC",
            model="Steuereinrichtung",
            firmware="unknown",
            device_type="grid_meter",
            discovery_sources=["eebus_ship_live"],
            protocols=["eebus_ship", "mdns"],
            evidence={
                "ship_service": {
                    "service_name": "CLS-Gateway._ship._tcp.local",
                    "target": "EPPCC001161952.local",
                    "port": 23292,
                    "path": "/ship/",
                    "ship_id": "i:32266_u:EPPCC001161952_r:Steuereinrichtung",
                    "ski": "f819e215a4f292d803325276767d9e27f67fe108",
                    "brand": "PPC",
                    "model": "Steuereinrichtung",
                    "device_type": "GCPH",
                    "register": register,
                    "addresses": {"ipv4": ["192.168.188.142"], "ipv6": []},
                    "tls_probe": None,
                },
                "identity_keys": [
                    "eebus-ski:f819e215a4f292d803325276767d9e27f67fe108",
                    "network-host:192-168-188-142",
                ],
                "supported_use_cases": ["limitationOfPowerConsumption", "limitationOfPowerProduction"],
            },
            classification_confidence=0.78,
            classification_reasoning="EEBus SHIP advertisement matched PPC SMGW.",
            state="classified",
            matched_device_id=device_id,
        )
        session.add(candidate)
        session.commit()


def _fake_eebus_peer_trust() -> EebusPeerTrustMaterial:
    return EebusPeerTrustMaterial(
        host="192.168.188.142",
        port=23292,
        server_name="EPPCC001161952.local",
        advertised_ski="f819e215a4f292d803325276767d9e27f67fe108",
        certificate_pem="-----BEGIN CERTIFICATE-----\nFAKE-TEST-CERT\n-----END CERTIFICATE-----\n",
        certificate_ski="f819e215a4f292d803325276767d9e27f67fe108",
        txt_ski_matches_certificate_ski=True,
        client_cert_requested=True,
        openssl_exit_code=1,
    )


class FakeEebusRuntimeManager:
    def __init__(self, status: str | list[str] = "listening", error: str = "") -> None:
        self.statuses = [status] if isinstance(status, str) else list(status)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def start_or_update(self, **kwargs) -> EebusRuntimeSnapshot:
        self.calls.append(kwargs)
        status = self.statuses[min(len(self.calls) - 1, len(self.statuses) - 1)]
        peer = kwargs["peer"]
        local_identity = kwargs["local_identity"]
        ready_peer_skis = [peer.certificate_ski] if status == "ship_ready" else []
        outbound_status = "ready" if status == "ship_ready" else "failed" if status == "failed" else "connecting"
        state_error = self.error if status == "failed" else ""
        return EebusRuntimeSnapshot(
            status=status,
            local_ski=local_identity.ski,
            local_ship_id=f"i:32266_u:HELIOS-HOME-HEMS_r:HEMS",
            bind_host="0.0.0.0",
            port=4712,
            path="/ship/",
            interface_ip="192.168.188.10",
            trusted_peer_skis=[peer.certificate_ski],
            ready_peer_skis=ready_peer_skis,
            endpoint_refs=[kwargs["endpoint_ref"]],
            diagnostic_run_refs=[kwargs["diagnostic_run_ref"]],
            active_connection_directions=["outbound_to_peer", "inbound_from_peer"],
            connection_states={
                kwargs["endpoint_ref"]: {
                    "outbound_to_peer": {
                        "status": outbound_status,
                        "host": peer.host,
                        "port": peer.port,
                        "path": peer.path,
                        "server_name": peer.server_name,
                        "peer_ski": peer.certificate_ski,
                        "error": state_error,
                    },
                    "inbound_from_peer": {
                        "status": "listening",
                        "bind_host": "0.0.0.0",
                        "port": 4712,
                        "path": "/ship/",
                    },
                }
            },
            error=state_error,
        )


def _endpoint_ref_for_device(device_id: str, protocol: str) -> str:
    session_factory = get_session_factory()
    with session_factory() as session:
        site = session.get(Site, _site_id())
        assert site is not None
        sync_inventory_to_home_graph(session, site.id)
        endpoint = session.scalar(
            select(ProtocolEndpoint).where(
                ProtocolEndpoint.owner_ref == f"device:{device_id}",
                ProtocolEndpoint.protocol == protocol,
            )
        )
        assert endpoint is not None
        return endpoint.id


def _send_and_stream(client: TestClient, content: str, context: dict[str, Any] | None = None) -> list[dict]:
    accepted = client.post("/api/v1/agent/messages", json={"content": content, "context": context or {}})
    assert accepted.status_code == 200
    response = client.get(f"/api/v1/agent/turns/{accepted.json()['turn_id']}/events")
    assert response.status_code == 200
    return _decode_sse_payloads(response.text)


def test_empty_thread_does_not_create_backend_authored_assistant_welcome(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "empty-thread.db")

    with TestClient(app) as client:
        response = client.get("/api/v1/agent/thread")

        assert response.status_code == 200
        assert response.json()["messages"] == []


def test_workstore_tasks_are_not_auto_rendered_or_injected_into_model_context(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "workstore-context.db")
    provider = _use_scripted_provider(monkeypatch, ScriptedModelProvider([ModelFinalAnswer("I will inspect state with tools when needed.")]))

    with TestClient(app) as client:
        thread_response = client.get("/api/v1/agent/thread")
        assert thread_response.status_code == 200
        thread_id = thread_response.json()["id"]

        session_factory = get_session_factory()
        with session_factory() as session:
            site = session.scalar(select(Site).limit(1))
            assert site is not None
            session.add(
                AgentTask(
                    id="task-stale-eebus",
                    site_id=site.id,
                    thread_id=thread_id,
                    task_type="commission_role_candidate",
                    title="commission_role_candidate",
                    goal="commission_role_candidate",
                    status="blocked",
                    target_refs=["device:dev-eebus"],
                    context={"current_phase": "waiting_for_ship_session"},
                )
            )
            session.add(
                Blocker(
                    id="blocker-stale-eebus",
                    task_id="task-stale-eebus",
                    subject_ref="device:dev-eebus",
                    blocker_type="eebus_peer_connection_pending",
                    summary="eebus_peer_connection_pending",
                    status="open",
                )
            )
            session.commit()

        thread_with_work = client.get("/api/v1/agent/thread")
        assert thread_with_work.status_code == 200
        payload = thread_with_work.json()
        assert "active_tasks" not in payload
        assert "open_blockers" not in payload

        events = _send_and_stream(client, "How many EEBUS devices are connected?")
        assert any(event["event_type"] == "assistant_message_completed" for event in events)
        assert provider.requests
        assert "active_tasks" not in provider.requests[0].context
        assert "open_blockers" not in provider.requests[0].context


def test_setup_profile_get_or_create_recovers_from_concurrent_insert(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "setup-profile-race.db")

    with TestClient(app):
        from app.agent.service import _get_or_create_setup_profile

        session_factory = get_session_factory()
        with session_factory() as session:
            site = session.scalar(select(Site).limit(1))
            assert site is not None
            assert session.scalar(select(SiteSetupProfile).where(SiteSetupProfile.site_id == site.id)) is None
            original_commit = session.commit
            state = {"raised": False}

            def commit_after_competing_insert() -> None:
                if state["raised"]:
                    original_commit()
                    return
                state["raised"] = True
                with session_factory() as competing_session:
                    competing_session.add(
                        SiteSetupProfile(
                            site_id=site.id,
                            summary="setup_profile_initialized",
                            confirmed_systems=[],
                            unresolved_items=[],
                            user_notes=[],
                        )
                    )
                    competing_session.commit()
                raise IntegrityError("insert into site_setup_profiles", {}, Exception("unique site setup profile"))

            monkeypatch.setattr(session, "commit", commit_after_competing_insert)

            profile = _get_or_create_setup_profile(session)

            assert profile.site_id == site.id
            assert state["raised"] is True
            profiles = session.scalars(select(SiteSetupProfile).where(SiteSetupProfile.site_id == site.id)).all()
            assert len(profiles) == 1


def test_openai_responses_native_final_text_maps_to_model_final_answer():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "I do not have days, but I am here and ready to help."}],
            }
        ]
    }

    action = _model_action_from_openai_responses_payload(payload, {}, force_final=False)

    assert isinstance(action, ModelFinalAnswer)
    assert action.content == "I do not have days, but I am here and ready to help."


def test_openai_responses_native_function_call_maps_to_internal_tool_name():
    available_tools = [{"name": "home_graph.query", "input_schema": {"type": "object", "properties": {}}}]
    name_map = _provider_tool_name_map(available_tools)
    payload = {
        "output": [
            {
                "type": "function_call",
                "name": "home_graph__query",
                "arguments": "{\"role_hypothesis\":\"ev_charger\"}",
            }
        ]
    }

    action = _model_action_from_openai_responses_payload(payload, name_map, force_final=False)

    assert isinstance(action, ModelToolCall)
    assert action.name == "home_graph.query"
    assert action.arguments == {"role_hypothesis": "ev_charger"}


def test_system_prompt_frames_ui_as_shared_workspace_without_hard_focus_rule():
    runtime = ProviderRuntimeStatus(
        selected_provider="openai",
        effective_provider="openai",
        ready=True,
        message="ready",
        state=ProviderState(
            provider_id="openai",
            model="gpt-5-mini",
            base_url=PROVIDER_SPECS["openai"].base_url_default,
            api_key="test",
        ),
        spec=PROVIDER_SPECS["openai"],
    )
    provider = LLMModelProvider(runtime)
    prompt = provider._system_prompt(
        ModelRequest(
            turn_id="turn-test",
            user_message="Which devices are meters?",
            recent_messages=[],
            context={},
            available_tools=[],
        )
    )

    assert "shared visual workspace" in prompt
    assert "do not treat UI actions as decoration" in prompt
    assert "always" not in prompt.lower()
    assert "ui.focus_entities" not in prompt


def test_ui_focus_entities_tool_purpose_is_neutral():
    registry = create_default_tool_registry()
    tool = registry.get("ui.focus_entities")
    assert tool is not None
    assert tool.purpose == (
        "Focuses or highlights entities in the shared workspace, so the user can see which devices, "
        "roles, tasks, or blockers you are referring to."
    )
    assert "should" not in tool.purpose.lower()
    assert "when" not in tool.purpose.lower()


def test_dashboard_ui_actions_are_registered_as_agent_tools():
    registry = create_default_tool_registry()

    assert registry.get("ui.open_device_details") is not None
    assert registry.get("ui.open_connection_overlay") is not None
    assert registry.get("connection.disconnect") is not None
    assert registry.get("load_control.configure_device") is not None
    assert registry.get("load_control.inspect_status") is not None


def test_connection_establish_hides_protocol_direction_from_agent_schema():
    registry = create_default_tool_registry()
    establish = registry.get("connection.establish")
    inspect = registry.get("connection.inspect_readiness")

    assert establish is not None
    assert inspect is not None
    establish_schema = establish.input_model.model_json_schema()
    inspect_schema = inspect.input_model.model_json_schema()
    assert "connection_direction" not in establish_schema.get("properties", {})
    assert "connection_direction" not in inspect_schema.get("properties", {})
    assert registry.get("commissioning.start_or_continue") is None


def test_load_control_config_action_updates_overview_and_agent_tool(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "load-control-action.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-wallbox", name="Mennekes Wallbox", device_type="wallbox", manufacturer="MENNEKES")

        action_response = client.post(
            "/api/v1/actions/load_control.configure_device",
            json={
                "input": {
                    "device_id": "dev-wallbox",
                    "participates_lpc": True,
                    "lpc_share_pct": 35,
                    "participates_lpp": True,
                    "lpp_share_pct": 15,
                }
            },
        )
        assert action_response.status_code == 200
        action = action_response.json()
        assert action["output"]["status"] == "configured"
        assert action["output"]["load_control"]["lpc_share_pct"] == 35
        assert action["ui_events"] == [
            {"event_type": "device.details.open", "payload": {"entity_ref": "device:dev-wallbox"}}
        ]

        overview_response = client.get("/api/v1/overview")
        assert overview_response.status_code == 200
        device = next(entry for entry in overview_response.json()["devices"] if entry["id"] == "dev-wallbox")
        assert device["load_control"]["participates_lpc"] is True
        assert device["load_control"]["lpc_share_pct"] == 35
        assert device["load_control"]["participates_lpp"] is True
        assert device["load_control"]["lpp_share_pct"] == 15


def test_load_control_status_tool_reports_active_constraint_duration(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "load-control-status-tool.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        response = client.post(
            "/api/v1/eebus/load-power-limits/distribute",
            json={
                "use_case": "lpc",
                "limit_watts": 6000,
                "duration_seconds": 600,
                "source": "test",
                "peer_ski": "0123456789abcdef0123456789abcdef01234567",
            },
        )
        assert response.status_code == 200
        provider = _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall("load_control.inspect_status", {}),
                    ModelFinalAnswer("The active LPC is 6 kW and still has time remaining."),
                ]
            ),
        )

        events = _send_and_stream(client, "Wie lange ist der LPC noch aktiv?")

    tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
    assert tool_finished["payload"]["tool_name"] == "load_control.inspect_status"
    result = tool_finished["payload"]["result"]
    assert result["active_constraint_count"] == 1
    assert result["constraints"][0]["limit_watts"] == 6000
    assert result["constraints"][0]["duration_seconds"] == 600
    assert 0 < result["constraints"][0]["remaining_seconds"] <= 600
    assert result["constraints"][0]["expires_at"]
    assert provider.requests[0].context["load_control"]["active_constraint_count"] == 1


def test_runtime_uses_model_actions_tool_observations_and_model_final_answer(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "runtime-loop.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("home_graph.query", {"role_hypothesis": "ev_charger"}),
                ModelToolCall("ui.focus_entities", {"entity_refs": ["device:dev-mennekes", "device:dev-evcc"], "mode": "highlight"}),
                ModelFinalAnswer("I found the current EV-charger candidates and focused them in the workspace."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-mennekes", name="CC612_2S0R_CC", device_type="wallbox", manufacturer="MENNEKES")
        _add_device(device_id="dev-evcc", name="EVCC_HEMS", device_type="wallbox", manufacturer="EVCC")

        events = _send_and_stream(client, "Kannst du die Wallbox finden?")

        event_types = [event["event_type"] for event in events]
        assert "agent_runtime_started" in event_types
        assert "model_request" in event_types
        assert "provider_response" in event_types
        assert "model_action" in event_types
        assert "model_action_validation" in event_types
        assert "tool_started" in event_types
        assert "tool_finished" in event_types
        assert "model_observation" in event_types
        assert "model_final_answer" in event_types

        runtime_started = next(event for event in events if event["event_type"] == "agent_runtime_started")
        assert "home_graph.query" in runtime_started["payload"]["available_tools"]
        assert "home_graph.get_entity_details" in runtime_started["payload"]["available_tools"]
        assert "protocol.list_endpoints" in runtime_started["payload"]["available_tools"]
        assert "connection.inspect_readiness" in runtime_started["payload"]["available_tools"]
        assert "connection.establish" in runtime_started["payload"]["available_tools"]
        assert "eebus.identity.get_or_create" in runtime_started["payload"]["available_tools"]
        assert "commissioning.start_or_continue" not in runtime_started["payload"]["available_tools"]
        assert "commissioning.get_log" in runtime_started["payload"]["available_tools"]
        assert "discovery.inspect_home_network" in runtime_started["payload"]["available_tools"]
        assert "ui.focus_entities" in runtime_started["payload"]["available_tools"]
        assert "ui.open_device_details" in runtime_started["payload"]["available_tools"]
        assert "ui.open_connection_overlay" in runtime_started["payload"]["available_tools"]
        assert "home_graph.find_system_role" not in runtime_started["payload"]["available_tools"]
        assert "confirmation.respond_to_pending_decision" not in runtime_started["payload"]["available_tools"]
        assert "ui.highlight_entities" not in runtime_started["payload"]["available_tools"]

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        assert tool_finished["payload"]["tool_name"] == "home_graph.query"
        assert tool_finished["payload"]["result"]["role_hypothesis"] == "ev_charger"
        assert tool_finished["payload"]["result"]["scope"] == "canonical_devices"
        assert {entry["ref"] for entry in tool_finished["payload"]["result"]["matching_entities"]} == {
            "device:dev-mennekes",
            "device:dev-evcc",
        }
        ui_events = [event for event in events if event["event_type"] == "ui_events"]
        assert any(
            ui_event["payload"]["events"][0]["event_type"] == "entity.focus"
            and sorted(ui_event["payload"]["events"][0]["payload"]["entity_refs"]) == ["device:dev-evcc", "device:dev-mennekes"]
            and ui_event["payload"]["events"][0]["payload"]["mode"] == "highlight"
            for ui_event in ui_events
        )

        completed = next(event for event in events if event["event_type"] == "assistant_message_completed")
        assert completed["payload"]["message"]["content"] == "I found the current EV-charger candidates and focused them in the workspace."
        assert "ui_actions" not in completed["payload"]
        assert len(provider.requests) == 3
        assert provider.requests[1].observations[0].tool_name == "home_graph.query"


def test_default_model_context_is_compact_and_tool_pull_based(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "compact-context.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider([ModelFinalAnswer("I can inspect details with tools when needed.")]),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        for index in range(25):
            _add_device(
                device_id=f"dev-context-{index}",
                name=f"Context Device {index}",
                device_type="smart_appliance",
                manufacturer="Test",
                protocols=["http_local"],
            )

        _send_and_stream(client, "What do you know right now?")

        context = provider.requests[0].context
        encoded_context = json.dumps(context, default=str)
        assert len(encoded_context) < 25000
        assert "home_graph_entities" not in context
        assert "home_graph_relationships" not in context
        assert "available_tools" not in context
        assert context["home_graph_summary"]["canonical_device_count"] == 25
        assert context["home_graph_summary"]["canonical_device_counts_by_type"] == {"smart_appliance": 25}
        assert context["home_graph_summary"]["observed_class_counts"] == {"local_http_endpoint": 25}
        assert context["home_graph_summary"]["role_hypothesis_counts"] == {}
        assert context["home_inventory"]["canonical_device_count"] == 25
        assert "canonical_devices" not in context["home_inventory"]
        assert context["home_inventory"]["primary_observations"][0]["classification_status"] == "unclassified"
        assert len(context["home_inventory"]["primary_observations"]) == 8
        assert context["home_graph_summary"]["details_available_via"] == [
            "home_graph.query",
            "home_graph.get_entity_details",
        ]
        assert context["home_graph_summary"]["normal_query_scope"] == "canonical_devices"


def test_home_graph_context_separates_canonical_devices_from_raw_candidates(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "canonical-inventory.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider([ModelFinalAnswer("I can see the canonical inventory.")]),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        _add_device(device_id="dev-fronius-meter", name="Smart Meter IP", device_type="grid_meter", manufacturer="Fronius")

        _send_and_stream(client, "What meters are known?")

        context = provider.requests[0].context
        assert context["home_graph_summary"]["canonical_device_count"] == 2
        assert context["home_graph_summary"]["canonical_device_counts_by_type"] == {"grid_meter": 2}
        assert context["home_graph_summary"]["role_hypothesis_counts"] == {"grid_meter": 2}
        assert context["home_graph_summary"]["raw_artifact_counts"]["candidate_count"] == 1
        assert context["home_graph_summary"]["raw_artifact_counts"]["candidate_counts_by_type"] == {"grid_meter": 1}


def test_followup_reference_resolution_uses_structured_context_not_backend_keyword_rewrite(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "followup.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("home_graph.query", {"role_hypothesis": "ev_charger"}),
                ModelFinalAnswer("I see two wallbox candidates."),
                ModelToolCall("home_graph.resolve_entity_reference", {"text": "ich meine die Mennekes Wallbox", "role": "ev_charger"}),
                ModelFinalAnswer("I focused the Mennekes wallbox candidate."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-mennekes", name="CC612_2S0R_CC", device_type="wallbox", manufacturer="MENNEKES")
        _add_wallbox_candidate("dev-mennekes", "cand-mennekes", "CC612_2S0R_CC")
        _add_device(device_id="dev-evcc", name="EVCC_HEMS", device_type="wallbox", manufacturer="EVCC")

        _send_and_stream(client, "Kannst du die Wallbox finden?")
        followup_events = _send_and_stream(client, "ich meine die Mennekes Wallbox")

        tool_finished = next(event for event in followup_events if event["event_type"] == "tool_finished")
        assert tool_finished["payload"]["tool_name"] == "home_graph.resolve_entity_reference"
        assert tool_finished["payload"]["result"]["found"] is True
        assert tool_finished["payload"]["result"]["resolved_entity"]["ref"] == "device:dev-mennekes"

        second_request = provider.requests[2]
        assert any(entry["role"] == "ev_charger" for entry in second_request.context["recent_candidate_sets"])


def test_binding_request_creates_proposal_and_decision_request_without_binding_application(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "proposal.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("device.assess", {"entity_ref": "device:dev-ppc", "question": "Bind the PPC SMGW"}),
                ModelToolCall(
                    "role.prepare_binding_proposal",
                    {
                        "entity_ref": "device:dev-ppc",
                        "role": "grid_meter",
                        "label": "PPC SMGW",
                        "rationale": "User asked to bind the PPC smart meter gateway.",
                    },
                ),
                ModelFinalAnswer("I prepared a proposal and need your decision before anything is applied."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        events = _send_and_stream(client, "Bitte das PPC SMGW als Smart Meter Gateway anbinden.")

        tool_names = [event["payload"]["tool_name"] for event in events if event["event_type"] == "tool_finished"]
        assert tool_names == ["device.assess", "role.prepare_binding_proposal"]
        assessment_event = next(event for event in events if event["event_type"] == "tool_finished" and event["payload"]["tool_name"] == "device.assess")
        assessment_result = assessment_event["payload"]["result"]
        assert "summary" not in assessment_result
        assert all("summary" not in evidence for evidence in assessment_result["evidence"])
        proposal_event = next(event for event in events if event["event_type"] == "proposal_created")
        decision_event = next(event for event in events if event["event_type"] == "decision_request_created")
        assert proposal_event["payload"]["action_type"] == "role_binding"
        assert proposal_event["payload"]["summary"] == "role_binding_proposal"
        assert proposal_event["payload"]["decision_question"] is None
        assert decision_event["payload"]["decision_request_id"]
        assert decision_event["payload"]["question"] == ""

        session_factory = get_session_factory()
        with session_factory() as session:
            assert session.scalar(select(Proposal)) is not None
            assert session.scalar(select(UserDecisionRequest)) is not None
            assert session.scalar(select(HemsSystemBinding)) is None


def test_entity_details_tool_returns_protocol_endpoints_without_backend_advice(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "entity-details.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("home_graph.get_entity_details", {"entity_ref": "device:dev-ppc"}),
                ModelFinalAnswer("I inspected the PPC SMGW details."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        events = _send_and_stream(client, "Schau dir das PPC SMGW an.")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        result = tool_finished["payload"]["result"]
        assert tool_finished["payload"]["tool_name"] == "home_graph.get_entity_details"
        assert result["entity"]["ref"] == "device:dev-ppc"
        assert result["canonical"] is True
        assert result["observed_identity"]["display_name"] == "PPC SMGW"
        assert result["classification"]["status"] == "tentative"
        assert result["classification"]["observed_class"] == "eebus_ship_peer"
        assert result["classification"]["role_hypotheses"][0]["role"] == "grid_meter"
        assert result["classification"]["role_hypotheses"][0]["needs_confirmation"] is True
        assert "eebus_ship" in result["technical_observations"]
        assert result["connection_facets"]["overall_connection_state"] == "endpoint_visible"
        assert result["connection_facets"]["facets"]["endpoint_state"] == "visible"
        assert "next_step" not in result["entity"]["properties"]
        assert "explanation" not in result["entity"]["properties"]
        assert result["protocol_endpoints"][0]["protocol"] == "eebus_ship"
        assert result["protocol_endpoints"][0]["port"] == 4711
        assert "recommended_option" not in result


def test_entity_details_keep_generic_smart_appliance_role_unclassified(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "entity-details-generic-appliance.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("home_graph.get_entity_details", {"entity_ref": "device:dev-shelly"}),
                ModelFinalAnswer("I inspected the visible local endpoint."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(
            device_id="dev-shelly",
            name="Shelly1PM",
            device_type="smart_appliance",
            manufacturer="Shelly",
            protocols=["http_local"],
        )
        events = _send_and_stream(client, "Was ist das Shelly?")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        result = tool_finished["payload"]["result"]
        assert result["classification"]["status"] == "unclassified"
        assert result["classification"]["observed_class"] == "local_http_endpoint"
        assert result["classification"]["role_hypotheses"] == []
        assert "http_local" in result["technical_observations"]


def test_home_graph_role_query_does_not_hard_filter_free_text_when_role_is_structured(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "role-query-soft-text.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall(
                    "home_graph.query",
                    {
                        "entity_types": ["device"],
                        "role_hypothesis": "grid_meter",
                        "text": "liste alle grid_meter device refs",
                        "include_relationships": False,
                    },
                ),
                ModelFinalAnswer("I found the canonical meter devices."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_device(device_id="dev-fronius-meter", name="Smart Meter IP", device_type="grid_meter", manufacturer="Fronius")

        events = _send_and_stream(client, "Welche Stromzähler gibt es?")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        result = tool_finished["payload"]["result"]
        assert {entry["ref"] for entry in result["matching_entities"]} == {
            "device:dev-ppc",
            "device:dev-fronius-meter",
        }
        assert result["relationships"] == []


def test_binding_proposal_records_model_selected_endpoint_and_integration_path(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "proposal-path.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "role.prepare_binding_proposal",
                        {
                            "entity_ref": "device:dev-ppc",
                            "role": "grid_meter",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "label": "PPC SMGW",
                            "rationale": "User asked to bind the PPC SMGW through EEBus.",
                        },
                    ),
                    ModelFinalAnswer("I prepared the EEBus binding proposal for your decision."),
                ]
            ),
        )

        _send_and_stream(client, "Bitte das PPC SMGW per EEBus als Smart Meter Gateway anbinden.")

        session_factory = get_session_factory()
        with session_factory() as session:
            proposal = session.scalar(select(Proposal))
            assert proposal is not None
            assert proposal.payload["entity_ref"] == "device:dev-ppc"
            assert proposal.payload["role"] == "grid_meter"
            assert proposal.payload["endpoint_ref"] == endpoint_ref
            assert proposal.payload["endpoint_protocol"] == "eebus_ship"
            assert proposal.payload["integration_path"] == "eebus_spine"
            assert proposal.payload["binding_scope"] == {
                "establishes_role_candidate_only": True,
                "establishes_protocol_connection": False,
                "validates_spine_or_telemetry": False,
            }
            assert proposal.payload["connection_facets_at_creation"]["overall_connection_state"] != "connected"
            assert endpoint_ref in proposal.target_refs
            assert session.scalar(select(HemsSystemBinding)) is None


def test_protocol_list_endpoints_exposes_allowed_integration_paths(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "protocol-endpoints.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("protocol.list_endpoints", {"entity_ref": "device:dev-ppc", "protocol": "eebus_ship"}),
                ModelFinalAnswer("I found the PPC EEBus endpoint."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)

        events = _send_and_stream(client, "List endpoints for the PPC SMGW.")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        assert tool_finished["payload"]["tool_name"] == "protocol.list_endpoints"
        endpoints = tool_finished["payload"]["result"]["endpoints"]
        assert len(endpoints) == 1
        assert endpoints[0]["protocol"] == "eebus_ship"
        assert endpoints[0]["allowed_integration_paths"] == ["eebus_spine"]
        assert endpoints[0]["properties"]["register"] is False


def test_connection_readiness_reports_eebus_trust_blockers_without_connecting(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "connection-readiness.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.inspect_readiness",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("The PPC is visible, but trust commissioning is blocked."),
                ]
            ),
        )

        events = _send_and_stream(client, "Can this PPC SMGW be connected through EEBus?")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        result = tool_finished["payload"]["result"]
        assert result["readiness"] == "blocked"
        assert result["connection_facets"]["overall_connection_state"] == "blocked"
        assert result["connection_facets"]["facets"]["trust_state"] == "required"
        assert result["diagnostic_run_ref"].startswith("protocol-diagnostic-")
        assert any(entry["event"] == "eebus_ship_metadata" for entry in result["log_entries"])
        blocker_codes = {blocker["code"] for blocker in result["blockers"]}
        assert "local_eebus_identity_missing" in blocker_codes
        assert "peer_certificate_not_materialized" in blocker_codes
        assert "ship_trust_commissioning_not_validated" in blocker_codes
        facts = result["inspections"][0]["facts"]
        assert facts["remote_register"] is False
        assert facts["remote_certificate_materialized"] is False
        assert facts["local_identity_exists"] is False
        assert any(transition["tool"] == "eebus.identity.get_or_create" for transition in result["available_transitions"])
        assert any(transition["tool"] == "connection.establish" for transition in result["available_transitions"])
        session_factory = get_session_factory()
        with session_factory() as session:
            diagnostic = session.get(ProtocolDiagnosticRun, result["diagnostic_run_ref"])
            assert diagnostic is not None
            assert diagnostic.status == "blocked"
            assert diagnostic.result["blocker_codes"]


def test_connection_readiness_distinguishes_peer_rejection_from_ship_ready(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "connection-readiness-peer-rejected.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        def runtime_snapshot(endpoint_id: str) -> dict[str, Any]:
            return {
                "status": "failed",
                "local_ski": "0123456789abcdef0123456789abcdef01234567",
                "endpoint_in_runtime": endpoint_id == endpoint_ref,
                "ready_peer_skis": [],
                "received_load_power_limit_count": 0,
                "last_load_power_limit": {},
                "recent_events": [{"reason": "Node rejected by application."}],
                "endpoint_connection_states": {
                    "outbound_to_peer": {
                        "status": "failed",
                        "error": "Node rejected by application.",
                    }
                },
                "error": "Node rejected by application.",
            }

        monkeypatch.setattr("app.agent.tools.protocol.runtime_snapshot_for_endpoint", runtime_snapshot)
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.inspect_readiness",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("The peer rejected the previous SHIP attempt; it is not connected yet."),
                ]
            ),
        )

        events = _send_and_stream(client, "Prüfe die PPC Verbindung.")

        result = next(event for event in events if event["event_type"] == "tool_finished")["payload"]["result"]
        blocker_codes = {blocker["code"] for blocker in result["blockers"]}
        facts = result["inspections"][0]["facts"]
        assert "eebus_peer_trust_required" in blocker_codes
        assert facts["ship_session_state"] == "not_ready"
        assert facts["spine_feature_exchange_state"] == "not_validated"
        assert facts["lpc_lpp_receive_state"] == "not_validated"


def test_eebus_identity_tool_creates_public_ski_without_exposing_private_key(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "eebus-identity.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("eebus.identity.get_or_create", {"common_name": "Helios Home HEMS"}),
                ModelFinalAnswer("I created the local EEBus identity."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        events = _send_and_stream(client, "Prepare the local EEBus identity.")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        identity = tool_finished["payload"]["result"]["identity"]
        assert len(identity["ski"]) == 40
        assert identity["certificate_pem"].startswith("-----BEGIN CERTIFICATE-----")
        certificate = x509.load_pem_x509_certificate(identity["certificate_pem"].encode("ascii"))
        subject_key_identifier = certificate.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_KEY_IDENTIFIER
        ).value.digest.hex()
        assert identity["ski"] == subject_key_identifier
        assert isinstance(certificate.public_key(), ec.EllipticCurvePublicKey)
        assert identity["ski_source"] == "x509_subject_key_identifier"
        assert identity["qr_payload"].startswith("ID:")
        assert "private_key_pem" not in identity
        assert tool_finished["payload"]["result"]["private_key_exported"] is False


def test_connection_establish_prepares_eebus_identity_and_records_trust_blocker(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-start.db")
    fake_runtime = FakeEebusRuntimeManager(status="listening")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")
        session_factory = get_session_factory()
        with session_factory() as session:
            site = session.get(Site, _site_id())
            assert site is not None
            stale_task = AgentTask(
                id="task-stale-commissioning",
                site_id=site.id,
                task_type="commission_role_candidate",
                title="Commission PPC SMGW",
                goal="Continue PPC SMGW commissioning.",
                status="open",
                target_refs=["device:dev-ppc", endpoint_ref],
                context={"current_phase": "awaiting_commissioning_workflow"},
            )
            stale_blocker = Blocker(
                id="blocker-stale-commissioning",
                task_id=stale_task.id,
                subject_ref="device:dev-ppc",
                blocker_type="commissioning_workflow_not_started",
                summary="commissioning_workflow_not_started",
                status="open",
                details={"missing_capabilities": ["eebus_trust_commissioning_readiness_validation"]},
            )
            session.add_all([stale_task, stale_blocker])
            session.commit()
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                            "reason": "User asked to continue connecting the PPC SMGW.",
                        },
                    ),
                    ModelFinalAnswer("I prepared the local SKI and found the peer trust blocker."),
                ]
            ),
        )

        events = _send_and_stream(client, "Mach mit dem PPC SMGW weiter.")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        result = tool_finished["payload"]["result"]
        assert tool_finished["payload"]["tool_name"] == "connection.establish"
        assert result["status"] == "connecting_ship_session"
        assert result["phase"] == "waiting_for_ship_session"
        assert len(result["local_identity"]["ski"]) == 40
        assert result["peer_certificate"]["status"] == "materialized"
        assert result["ship_runtime"]["status"] == "listening"
        assert result["required_user_action"]["local_ski"] == result["local_identity"]["ski"]
        assert result["required_user_action"]["retry_tool"] == "connection.establish"
        assert result["connection_facets"]["overall_connection_state"] == "blocked"
        assert "no_spine_feature_validation" in result["effects_not_included"]
        assert fake_runtime.calls
        assert fake_runtime.calls[0]["connection_direction"] == "auto"
        assert fake_runtime.calls[0]["peer"].host == "192.168.188.142"
        assert fake_runtime.calls[0]["peer"].port == 23292
        assert fake_runtime.calls[0]["peer"].server_name == "EPPCC001161952.local"

        session_factory = get_session_factory()
        with session_factory() as session:
            diagnostic = session.get(ProtocolDiagnosticRun, result["diagnostic_run_ref"])
            assert diagnostic is not None
            assert diagnostic.result["blocker_codes"] == ["eebus_peer_connection_pending"]
            blocker = session.scalar(select(Blocker).where(Blocker.blocker_type == "eebus_peer_connection_pending"))
            assert blocker is not None
            assert blocker.status == "open"
            stale_blocker = session.get(Blocker, "blocker-stale-commissioning")
            assert stale_blocker is not None
            assert stale_blocker.status == "resolved"
            assert stale_blocker.resolved_at is not None
            assert stale_blocker.details["resolved_by"] == "connection.establish"


def test_dashboard_connection_action_uses_same_connection_workflow_as_agent_tool(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "dashboard-connection-action.db")
    fake_runtime = FakeEebusRuntimeManager(status="failed", error="Node rejected by application.")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        options_response = client.get("/api/v1/devices/dev-ppc/connection-options")
        assert options_response.status_code == 200
        options = options_response.json()
        assert options["entity_ref"] == "device:dev-ppc"
        assert options["endpoints"][0]["endpoint_ref"] == endpoint_ref
        assert options["endpoints"][0]["connect_action"]["name"] == "connection.establish"
        assert options["endpoints"][0]["connect_action"]["input"] == {
            "entity_ref": "device:dev-ppc",
            "endpoint_ref": endpoint_ref,
            "integration_path": "eebus_spine",
        }

        action_response = client.post(
            "/api/v1/actions/connection.establish",
            json={
                "input": {
                    "entity_ref": "device:dev-ppc",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "eebus_spine",
                }
            },
        )

        assert action_response.status_code == 200
        action_result = action_response.json()
        assert action_result["actor"] == "user"
        assert action_result["action_name"] == "connection.establish"
        assert action_result["output"]["phase"] == "waiting_for_user_trust"
        assert action_result["output"]["required_user_action"]["local_ski"]
        assert action_result["ui_events"] == [
            {
                "event_type": "connection.overlay.open",
                "payload": {
                    "entity_ref": "device:dev-ppc",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "eebus_spine",
                    "mode": "waiting_for_user_trust",
                },
            }
        ]

        state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-ppc",
                "endpoint_ref": endpoint_ref,
                "integration_path": "eebus_spine",
            },
        )
        assert state_response.status_code == 200
        state = state_response.json()
        assert state["phase"] == "waiting_for_user_trust"
        assert state["protocol"] == "eebus_ship"
        assert state["host"] == "192.168.188.142"
        assert state["port"] == 23292
        assert state["last_error"] == ""
        assert state["connect_action"]["name"] == "connection.establish"
        assert any(step["key"] == "local_identity" and step["status"] == "completed" for step in state["steps"])
        assert any(step["key"] == "peer_trust" and step["status"] == "action_required" for step in state["steps"])
        assert any(step["key"] == "ship_session" and step["status"] == "pending" for step in state["steps"])

        session_factory = get_session_factory()
        with session_factory() as session:
            audit_actions = [
                row.action
                for row in session.scalars(
                    select(AuditEvent).where(AuditEvent.target_id == "connection.establish").order_by(AuditEvent.id)
                ).all()
            ]
            assert audit_actions == ["start_action", "complete_action"]


def test_http_connection_action_connects_and_disconnects_local_endpoint(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "dashboard-http-connection-action.db")

    def fake_refresh_http_telemetry(session, device, endpoint, *, now=None, timeout_seconds=None):
        assert device is not None
        device.telemetry = {"switch_0_power_w": 42, "switch_0_voltage_v": 239.9}
        device.telemetry_status = "live"
        device.telemetry_updated_at = now
        device.last_seen_at = now
        session.add(device)
        return HttpTelemetryProbeResult(
            status="updated",
            telemetry=device.telemetry,
            source="shelly_http",
            message="Shelly local HTTP telemetry sample received.",
        )

    monkeypatch.setattr("app.actions.service.refresh_http_device_telemetry", fake_refresh_http_telemetry)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(
            device_id="dev-http-load",
            name="HTTP Load",
            device_type="smart_appliance",
            manufacturer="Test",
            protocols=["http_local", "mdns"],
        )
        session_factory = get_session_factory()
        with session_factory() as session:
            device = session.get(Device, "dev-http-load")
            assert device is not None
            device.capabilities = {
                "visible": True,
                "monitorable": True,
                "controllable": True,
                "optimizable": False,
            }
            session.add(device)
            session.commit()

        endpoint_ref = _endpoint_ref_for_device("dev-http-load", "http_local")

        options_response = client.get("/api/v1/devices/dev-http-load/connection-options")
        assert options_response.status_code == 200
        options = options_response.json()
        assert [endpoint["protocol"] for endpoint in options["endpoints"]] == ["http_local"]
        assert options["endpoints"][0]["connect_action"]["name"] == "connection.establish"

        connect_response = client.post(
            "/api/v1/actions/connection.establish",
            json={
                "input": {
                    "entity_ref": "device:dev-http-load",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "http_local",
                }
            },
        )
        assert connect_response.status_code == 200
        assert connect_response.json()["output"]["status"] == "connected_http_ready"
        assert connect_response.json()["output"]["message"] == "Local HTTP telemetry path is live."

        state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-http-load",
                "endpoint_ref": endpoint_ref,
                "integration_path": "http_local",
            },
        )
        assert state_response.status_code == 200
        state = state_response.json()
        assert state["status"] == "connected_http_ready"
        assert state["connect_action"] is None
        assert state["disconnect_action"]["name"] == "connection.disconnect"

        with session_factory() as session:
            endpoint = session.get(ProtocolEndpoint, endpoint_ref)
            assert endpoint is not None
            endpoint.status = "observed"
            session.add(endpoint)
            session.commit()

        stale_state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-http-load",
                "endpoint_ref": endpoint_ref,
                "integration_path": "http_local",
            },
        )
        assert stale_state_response.status_code == 200
        stale_state = stale_state_response.json()
        assert stale_state["status"] == "ready_http_adapter"
        assert stale_state["connect_action"]["name"] == "connection.establish"
        assert stale_state["disconnect_action"] is None

        reconnect_response = client.post(
            "/api/v1/actions/connection.establish",
            json={
                "input": {
                    "entity_ref": "device:dev-http-load",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "http_local",
                }
            },
        )
        assert reconnect_response.status_code == 200
        assert reconnect_response.json()["output"]["status"] == "connected_http_ready"

        overview_response = client.get("/api/v1/overview")
        assert overview_response.status_code == 200
        overview_device = next(device for device in overview_response.json()["devices"] if device["id"] == "dev-http-load")
        assert overview_device["telemetry_status"] == "live"
        assert overview_device["telemetry"]["switch_0_power_w"] == 42
        assert overview_device["telemetry_updated_at"] is not None

        disconnect_response = client.post(
            "/api/v1/actions/connection.disconnect",
            json={
                "input": {
                    "entity_ref": "device:dev-http-load",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "http_local",
                }
            },
        )
        assert disconnect_response.status_code == 200
        assert disconnect_response.json()["output"]["status"] == "disconnected"

        disconnected_state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-http-load",
                "endpoint_ref": endpoint_ref,
                "integration_path": "http_local",
            },
        )
        assert disconnected_state_response.status_code == 200
        disconnected_state = disconnected_state_response.json()
        assert disconnected_state["status"] == "disconnected"
        assert disconnected_state["connect_action"]["name"] == "connection.establish"
        assert disconnected_state["disconnect_action"] is None

        overview_response = client.get("/api/v1/overview")
        assert overview_response.status_code == 200
        overview_device = next(device for device in overview_response.json()["devices"] if device["id"] == "dev-http-load")
        assert "connected" not in overview_device["status_tags"]
        assert "http_ready" not in overview_device["status_tags"]
        assert overview_device["telemetry_status"] == "sampled"


def test_modbus_connection_action_connects_sunspec_endpoint_and_updates_overview(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "dashboard-modbus-connection-action.db")

    def fake_probe_modbus_host(host: str, timeout_seconds: float):
        assert host == "198.51.100.90"
        return ModbusProbeResult(
            host=host,
            unit_id=1,
            vendor_name="Fronius",
            product_code="GEN24 Plus",
            revision="1.28.4",
            sunspec_base_register=40000,
            sunspec_model_ids=[1, 103],
            telemetry={
                "power_kw": 5.432,
                "energy_total_kwh": 123.456,
                "voltage_v": 230.0,
            },
        )

    monkeypatch.setattr("app.services.modbus_telemetry.probe_modbus_host", fake_probe_modbus_host)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(
            device_id="dev-fronius",
            name="Fronius Inverter",
            device_type="pv_inverter",
            manufacturer="Fronius",
            model="GEN24 Plus",
            protocols=["modbus_tcp"],
        )
        session_factory = get_session_factory()
        with session_factory() as session:
            site = session.get(Site, _site_id())
            assert site is not None
            session.add(
                DeviceCandidate(
                    id="cand-fronius-modbus",
                    site_id=site.id,
                    stable_key="dev-fronius",
                    display_name="Fronius Inverter",
                    manufacturer="Fronius",
                    model="GEN24 Plus",
                    firmware="1.28.4",
                    device_type="pv_inverter",
                    discovery_sources=["modbus_live"],
                    protocols=["modbus_tcp"],
                    evidence={
                        "identity_keys": ["network-host:198-51-100-90"],
                        "modbus_host": "198.51.100.90",
                        "modbus_port": 502,
                        "modbus_unit_id": 1,
                        "sunspec_base_register": 40000,
                        "sunspec_model_ids": [1, 103],
                    },
                    classification_confidence=0.9,
                    classification_reasoning="SunSpec inverter telemetry exposed production-side metrics.",
                    state="classified",
                    matched_device_id="dev-fronius",
                )
            )
            session.commit()

        endpoint_ref = _endpoint_ref_for_device("dev-fronius", "modbus_tcp")

        options_response = client.get("/api/v1/devices/dev-fronius/connection-options")
        assert options_response.status_code == 200
        modbus_endpoint = next(endpoint for endpoint in options_response.json()["endpoints"] if endpoint["protocol"] == "modbus_tcp")
        assert modbus_endpoint["endpoint_ref"] == endpoint_ref
        assert modbus_endpoint["connect_action"]["input"]["integration_path"] == "sunspec_modbus"

        connect_response = client.post(
            "/api/v1/actions/connection.establish",
            json={
                "input": {
                    "entity_ref": "device:dev-fronius",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "sunspec_modbus",
                }
            },
        )
        assert connect_response.status_code == 200
        assert connect_response.json()["output"]["status"] == "connected_sunspec_ready"
        assert connect_response.json()["output"]["telemetry_keys"] == ["energy_total_kwh", "power_kw", "voltage_v"]

        state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-fronius",
                "endpoint_ref": endpoint_ref,
                "integration_path": "sunspec_modbus",
            },
        )
        assert state_response.status_code == 200
        state = state_response.json()
        assert state["status"] == "connected_sunspec_ready"
        assert state["connect_action"] is None
        assert state["disconnect_action"]["name"] == "connection.disconnect"
        assert any(step["key"] == "sunspec_signature" and step["status"] == "completed" for step in state["steps"])

        overview_response = client.get("/api/v1/overview")
        assert overview_response.status_code == 200
        overview_device = next(device for device in overview_response.json()["devices"] if device["id"] == "dev-fronius")
        assert overview_device["telemetry_status"] == "live"
        assert overview_device["telemetry"]["power_kw"] == 5.432
        assert "connected" in overview_device["status_tags"]


def test_connection_options_materialize_modbus_endpoint_for_known_local_host(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "known-host-modbus-materialization.db")

    def fake_probe_modbus_host(host: str, timeout_seconds: float):
        assert host == "198.51.100.91"
        return ModbusProbeResult(
            host=host,
            unit_id=1,
            vendor_name="Fronius",
            product_code="GEN24 Plus",
            revision="1.28.4",
            sunspec_base_register=40000,
            sunspec_model_ids=[1, 103],
            telemetry={"power_kw": 3.21},
        )

    monkeypatch.setattr("app.services.modbus_telemetry.probe_modbus_host", fake_probe_modbus_host)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(
            device_id="dev-fronius-http",
            name="Fronius Inverter",
            device_type="pv_inverter",
            manufacturer="Fronius",
            model="Fronius Inverter",
            protocols=["http_local"],
        )
        session_factory = get_session_factory()
        with session_factory() as session:
            site = session.get(Site, _site_id())
            assert site is not None
            session.add(
                DeviceCandidate(
                    id="cand-fronius-http",
                    site_id=site.id,
                    stable_key="dev-fronius-http",
                    display_name="Fronius Inverter",
                    manufacturer="Fronius",
                    model="Fronius Inverter",
                    firmware="unknown",
                    device_type="pv_inverter",
                    discovery_sources=["local_network_live"],
                    protocols=["http_local"],
                    evidence={
                        "identity_keys": ["network-host:198-51-100-91"],
                        "http_host": "198.51.100.91",
                        "fingerprint_profile": "generic_http_energy",
                    },
                    classification_confidence=0.88,
                    classification_reasoning="Local HTTP fingerprint matched Fronius inverter.",
                    state="classified",
                    matched_device_id="dev-fronius-http",
                )
            )
            session.commit()

        options_response = client.get("/api/v1/devices/dev-fronius-http/connection-options")
        assert options_response.status_code == 200
        protocols = {endpoint["protocol"] for endpoint in options_response.json()["endpoints"]}
        assert protocols == {"http_local", "modbus_tcp"}
        modbus_endpoint = next(endpoint for endpoint in options_response.json()["endpoints"] if endpoint["protocol"] == "modbus_tcp")
        assert modbus_endpoint["host"] == "198.51.100.91"
        assert modbus_endpoint["connect_action"]["input"]["integration_path"] == "sunspec_modbus"

        overview_response = client.get("/api/v1/overview")
        overview_device = next(device for device in overview_response.json()["devices"] if device["id"] == "dev-fronius-http")
        assert "modbus_tcp" in overview_device["protocols"]
        assert overview_device["telemetry"]["power_kw"] == 3.21


def test_overview_reports_stale_live_telemetry_without_hiding_sample(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "dashboard-stale-http-telemetry.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(
            device_id="dev-http-stale",
            name="HTTP Stale Load",
            device_type="smart_appliance",
            manufacturer="Shelly",
            protocols=["http_local"],
        )
        session_factory = get_session_factory()
        with session_factory() as session:
            device = session.get(Device, "dev-http-stale")
            assert device is not None
            device.telemetry = {"switch_0_power_w": 11}
            device.telemetry_status = "live"
            device.telemetry_updated_at = utcnow() - timedelta(seconds=60)
            session.add(device)
            session.commit()

        overview_response = client.get("/api/v1/overview")

    assert overview_response.status_code == 200
    overview_device = next(device for device in overview_response.json()["devices"] if device["id"] == "dev-http-stale")
    assert overview_device["telemetry_status"] == "stale"
    assert overview_device["telemetry"]["switch_0_power_w"] == 11


def test_connection_state_reports_ship_runtime_errors_without_trust_masking(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "dashboard-connection-port-conflict.db")
    runtime_error = "[Errno 98] error while attempting to bind on address ('0.0.0.0', 4712): address already in use"
    fake_runtime = FakeEebusRuntimeManager(status="failed", error=runtime_error)
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        action_response = client.post(
            "/api/v1/actions/connection.establish",
            json={
                "input": {
                    "entity_ref": "device:dev-ppc",
                    "endpoint_ref": endpoint_ref,
                    "integration_path": "eebus_spine",
                }
            },
        )

        assert action_response.status_code == 200
        action_result = action_response.json()
        assert action_result["output"]["phase"] == "ship_failed"
        assert action_result["output"]["status"] == "failed_ship_runtime"
        assert action_result["output"]["required_user_action"]["action"] == (
            "resolve_ship_runtime_error_then_retry_connection_establish"
        )
        assert "address already in use" in action_result["output"]["required_user_action"]["last_error"]

        state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-ppc",
                "endpoint_ref": endpoint_ref,
                "integration_path": "eebus_spine",
            },
        )

        assert state_response.status_code == 200
        state = state_response.json()
        assert state["phase"] == "ship_failed"
        assert "address already in use" in state["last_error"]
        assert state["required_user_action"]["action"] == "resolve_ship_runtime_error_then_continue"
        peer_trust = next(step for step in state["steps"] if step["key"] == "peer_trust")
        ship_session = next(step for step in state["steps"] if step["key"] == "ship_session")
        assert peer_trust["status"] == "pending"
        assert ship_session["status"] == "failed"
        assert "address already in use" in ship_session["detail"]


def test_connection_establish_continues_after_user_trust_registration(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-trust-retry.db")
    fake_runtime = FakeEebusRuntimeManager(
        status=["failed", "ship_ready"],
        error="Node rejected by application.",
    )
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("Please add the local SKI and tell me when it is accepted."),
                ]
            ),
        )
        first_events = _send_and_stream(client, "Verbinde das PPC SMGW.")
        first_result = next(event for event in first_events if event["event_type"] == "tool_finished")["payload"]["result"]

        assert first_result["phase"] == "waiting_for_user_trust"
        assert first_result["status"] == "waiting_for_user_trust"
        assert first_result["ship_runtime"]["status"] == "failed"
        assert first_result["required_user_action"]["retry_tool"] == "connection.establish"
        assert first_result["required_user_action"]["ship_ready"] is False
        assert first_result["connection_lifecycle"]["verified_states"]["ship_session"] == "not_ready"

        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("SHIP is now verified by the connection workflow."),
                ]
            ),
        )
        second_events = _send_and_stream(client, "Die SKI wurde akzeptiert, ACK 0.")
        second_result = next(event for event in second_events if event["event_type"] == "tool_finished")["payload"]["result"]

        assert second_result["phase"] == "ship_ready"
        assert second_result["status"] == "connected_ship_ready"
        assert second_result["connection_lifecycle"]["previous_phase"] == "waiting_for_user_trust"
        assert second_result["connection_lifecycle"]["connection_attempt_count"] == 2
        assert second_result["connection_lifecycle"]["verified_states"]["ship_session"] == "ready"
        assert len(fake_runtime.calls) == 2

        session_factory = get_session_factory()
        with session_factory() as session:
            task = session.scalar(select(AgentTask).where(AgentTask.task_type == "commission_role_candidate"))
            assert task is not None
            assert task.context["connection_attempt_count"] == 2
            trust_blocker = session.scalar(select(Blocker).where(Blocker.blocker_type == "eebus_peer_trust_required"))
            assert trust_blocker is not None
            assert trust_blocker.status == "resolved"


def test_readiness_preserves_materialized_eebus_peer_after_inventory_sync(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-readiness-preserves-peer.db")
    fake_runtime = FakeEebusRuntimeManager(status="listening")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        def runtime_snapshot(endpoint_id: str) -> dict[str, Any]:
            return {
                "status": "listening",
                "local_ski": "0123456789abcdef0123456789abcdef01234567",
                "local_ship_id": "i:32266_u:HELIOS-HOME-HEMS_r:HEMS",
                "bind_host": "0.0.0.0",
                "port": 4712,
                "path": "/ship/",
                "interface_ip": "192.168.188.10",
                "trusted_peer_skis": ["f819e215a4f292d803325276767d9e27f67fe108"],
                "ready_peer_skis": [],
                "endpoint_refs": [endpoint_ref],
                "diagnostic_run_refs": [],
                "recent_events": [],
                "received_load_power_limit_count": 0,
                "last_load_power_limit": {},
                "error": "",
                "endpoint_in_runtime": endpoint_id == endpoint_ref,
            }

        monkeypatch.setattr("app.agent.tools.protocol.runtime_snapshot_for_endpoint", runtime_snapshot)
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelToolCall(
                        "connection.inspect_readiness",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("The peer certificate is still known; SHIP is waiting for a session."),
                ]
            ),
        )

        events = _send_and_stream(client, "Starte PPC Commissioning und prüfe danach die Readiness.")

        tool_results = [event["payload"]["result"] for event in events if event["event_type"] == "tool_finished"]
        assert tool_results[0]["peer_certificate"]["status"] == "materialized"
        readiness = tool_results[1]
        blocker_codes = {blocker["code"] for blocker in readiness["blockers"]}
        facts = readiness["inspections"][0]["facts"]
        assert facts["remote_certificate_materialized"] is True
        assert facts["remote_certificate_ski"] == "f819e215a4f292d803325276767d9e27f67fe108"
        assert "peer_certificate_not_materialized" not in blocker_codes
        assert "ship_trust_commissioning_not_validated" in blocker_codes

        session_factory = get_session_factory()
        with session_factory() as session:
            sync_inventory_to_home_graph(session, _site_id())
            endpoint = session.get(ProtocolEndpoint, endpoint_ref)
            assert endpoint is not None
            assert endpoint.properties["peer_certificate_ski"] == "f819e215a4f292d803325276767d9e27f67fe108"
            assert endpoint.properties["tls_probe"]["cert_ski"] == "f819e215a4f292d803325276767d9e27f67fe108"


def test_connection_establish_reports_ship_ready_without_legacy_trust_blocker(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-ready.db")
    fake_runtime = FakeEebusRuntimeManager(status="ship_ready")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        def ready_runtime_snapshot(endpoint_id: str) -> dict[str, Any]:
            return {
                "status": "ship_ready",
                "endpoint_refs": [endpoint_ref],
                "endpoint_in_runtime": endpoint_id == endpoint_ref,
                "ready_peer_skis": ["f819e215a4f292d803325276767d9e27f67fe108"],
                "received_load_power_limit_count": 1,
                "endpoint_connection_states": {
                    "outbound_to_peer": {"status": "ready"},
                    "inbound_from_peer": {"status": "ready"},
                },
                "connection_states": {
                    endpoint_ref: {
                        "outbound_to_peer": {"status": "ready"},
                        "inbound_from_peer": {"status": "ready"},
                    }
                },
                "recent_events": [],
                "error": "",
            }

        monkeypatch.setattr("app.home_graph.service.runtime_snapshot_for_endpoint", ready_runtime_snapshot)
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("SHIP is ready."),
                ]
            ),
        )

        events = _send_and_stream(client, "Verbinde das PPC SMGW weiter.")

        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        result = tool_finished["payload"]["result"]
        assert result["status"] == "connected_ship_ready"
        assert result["phase"] == "ship_ready"
        assert result["ship_runtime"]["ready_peer_skis"] == ["f819e215a4f292d803325276767d9e27f67fe108"]
        assert "no_spine_feature_validation" not in result["effects_not_included"]
        session_factory = get_session_factory()
        with session_factory() as session:
            diagnostic = session.get(ProtocolDiagnosticRun, result["diagnostic_run_ref"])
            assert diagnostic is not None
            assert diagnostic.result["blocker_codes"] == []

        overview = client.get("/api/v1/overview").json()
        device = next(row for row in overview["devices"] if row["id"] == "dev-ppc")
        assert device["primary_status"] == "connected"
        assert "connected" in device["status_tags"]
        assert "eebus_ship_ready" in device["status_tags"]


def test_connection_state_does_not_use_stale_ship_ready_diagnostic_as_live_state(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-stale-ready.db")
    fake_runtime = FakeEebusRuntimeManager(status="ship_ready")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("SHIP is ready."),
                ]
            ),
        )
        _send_and_stream(client, "Verbinde das PPC SMGW weiter.")

        def not_started_runtime_snapshot(endpoint_id: str) -> dict[str, Any]:
            return {
                "status": "not_started",
                "endpoint_refs": [],
                "endpoint_in_runtime": False,
                "ready_peer_skis": [],
                "received_load_power_limit_count": 0,
                "endpoint_connection_states": {},
                "connection_states": {},
                "recent_events": [],
                "error": "",
            }

        monkeypatch.setattr("app.actions.service.runtime_snapshot_for_endpoint", not_started_runtime_snapshot)
        monkeypatch.setattr("app.home_graph.service.runtime_snapshot_for_endpoint", not_started_runtime_snapshot)

        state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-ppc",
                "endpoint_ref": endpoint_ref,
                "integration_path": "eebus_spine",
            },
        )

        assert state_response.status_code == 200
        state = state_response.json()
        assert state["phase"] != "ship_ready"
        assert state["status"] != "connected_ship_ready"
        assert not state["required_user_action"].get("action", "").startswith("authorize_local_ski")
        assert state["connection_facets"]["overall_connection_state"] == "endpoint_visible"
        assert next(step for step in state["steps"] if step["key"] == "ship_session")["status"] == "pending"


def test_connection_state_does_not_show_stale_authorize_action_when_endpoint_peer_is_ready(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-stale-authorize.db")
    fake_runtime = FakeEebusRuntimeManager(status="ship_ready")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr("app.workflows.eebus_connection.get_eebus_runtime_manager", lambda: fake_runtime)

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")

        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                            "role": "grid_meter",
                        },
                    ),
                    ModelFinalAnswer("SHIP is ready."),
                ]
            ),
        )
        events = _send_and_stream(client, "Verbinde das PPC SMGW.")
        result = next(event for event in events if event["event_type"] == "tool_finished")["payload"]["result"]
        local_ski = result["local_identity"]["ski"]

        stale_ready_runtime = {
            "status": "ship_ready",
            "endpoint_refs": [endpoint_ref],
            "endpoint_in_runtime": True,
            "ready_peer_skis": ["f819e215a4f292d803325276767d9e27f67fe108"],
            "received_load_power_limit_count": 0,
            "endpoint_connection_states": {
                "outbound_to_peer": {
                    "status": "starting",
                    "peer_ski": "f819e215a4f292d803325276767d9e27f67fe108",
                    "error": "websocket closed by remote: code=4200 reason='shutdown'",
                }
            },
            "connection_states": {
                endpoint_ref: {
                    "outbound_to_peer": {
                        "status": "starting",
                        "peer_ski": "f819e215a4f292d803325276767d9e27f67fe108",
                        "error": "websocket closed by remote: code=4200 reason='shutdown'",
                    }
                }
            },
            "recent_events": [{"reason": "websocket closed by remote: code=4200 reason='shutdown'"}],
            "error": "websocket closed by remote: code=4200 reason='shutdown'",
        }
        session_factory = get_session_factory()
        with session_factory() as session:
            diagnostic = session.get(ProtocolDiagnosticRun, result["diagnostic_run_ref"])
            assert diagnostic is not None
            diagnostic.result = {
                **(diagnostic.result or {}),
                "phase": "waiting_for_ship_session",
                "status": "ship_session_pending",
                "runtime": stale_ready_runtime,
                "required_user_action": {
                    "action": "authorize_local_ski_on_peer_then_retry_connection_establish",
                    "local_ski": local_ski,
                    "peer_ski": "f819e215a4f292d803325276767d9e27f67fe108",
                },
            }
            session.add(diagnostic)
            session.commit()

        def stale_ready_snapshot(endpoint_id: str) -> dict[str, Any]:
            return dict(stale_ready_runtime, endpoint_in_runtime=endpoint_id == endpoint_ref)

        monkeypatch.setattr("app.actions.service.runtime_snapshot_for_endpoint", stale_ready_snapshot)
        monkeypatch.setattr("app.home_graph.service.runtime_snapshot_for_endpoint", stale_ready_snapshot)

        state_response = client.get(
            "/api/v1/connections/state",
            params={
                "entity_ref": "device:dev-ppc",
                "endpoint_ref": endpoint_ref,
                "integration_path": "eebus_spine",
            },
        )

        assert state_response.status_code == 200
        state = state_response.json()
        assert state["phase"] == "ship_ready"
        assert state["status"] == "connected_ship_ready"
        assert state["required_user_action"] == {}
        assert state["last_error"] == ""
        assert state["connect_action"] is None
        assert state["disconnect_action"] is not None
        assert state["connection_facets"]["overall_connection_state"] == "ship_ready"


def test_commissioning_get_log_reads_compact_diagnostic_entries(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-log.db")
    monkeypatch.setattr("app.workflows.eebus_connection.probe_eebus_peer_certificate", lambda **_: _fake_eebus_peer_trust())
    monkeypatch.setattr(
        "app.workflows.eebus_connection.get_eebus_runtime_manager",
        lambda: FakeEebusRuntimeManager(status="listening"),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        _add_ppc_eebus_candidate("dev-ppc", register=False)
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "connection.establish",
                        {
                            "entity_ref": "device:dev-ppc",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "eebus_spine",
                        },
                    ),
                    ModelToolCall("commissioning.get_log", {"entity_ref": "device:dev-ppc"}),
                    ModelFinalAnswer("I read the commissioning log."),
                ]
            ),
        )

        events = _send_and_stream(client, "Starte und lies das PPC Commissioning Log.")

        tool_finished = [event for event in events if event["event_type"] == "tool_finished"]
        log_result = tool_finished[-1]["payload"]["result"]
        assert tool_finished[-1]["payload"]["tool_name"] == "commissioning.get_log"
        assert len(log_result["diagnostic_runs"]) == 1
        events = {entry["event"] for entry in log_result["diagnostic_runs"][0]["log_entries"]}
        assert "peer_certificate_materialized" in events
        assert "eebus_connection_workflow_result" in events


def test_binding_proposal_rejects_incompatible_selected_endpoint_path(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "proposal-path-invalid.db")

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        endpoint_ref = _endpoint_ref_for_device("dev-ppc", "eebus_ship")
        _use_scripted_provider(
            monkeypatch,
            ScriptedModelProvider(
                [
                    ModelToolCall(
                        "role.prepare_binding_proposal",
                        {
                            "entity_ref": "device:dev-ppc",
                            "role": "grid_meter",
                            "endpoint_ref": endpoint_ref,
                            "integration_path": "modbus_tcp",
                            "label": "PPC SMGW",
                            "rationale": "Invalid path test.",
                        },
                    ),
                    ModelFinalAnswer("That endpoint cannot be used through Modbus/TCP."),
                ]
            ),
        )

        events = _send_and_stream(client, "Bitte das PPC SMGW per Modbus anbinden.")

        assert any(event["event_type"] == "tool_failed" for event in events)
        observation = next(event for event in events if event["event_type"] == "model_observation")
        assert "not compatible" in observation["payload"]["error"]
        session_factory = get_session_factory()
        with session_factory() as session:
            assert session.scalar(select(Proposal)) is None


def test_model_cannot_approve_decisions_by_tool_call(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "no-confirmation-tool.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("confirmation.respond_to_pending_decision", {"decision_request_ref": "decision-x", "decision": "approve"}),
                ModelFinalAnswer("I cannot approve that decision directly."),
            ]
        ),
    )

    with TestClient(app) as client:
        events = _send_and_stream(client, "ja")
        assert any(event["event_type"] == "model_action_validation" and event["payload"]["valid"] is False for event in events)
        assert any(event["event_type"] == "model_observation" and event["payload"].get("error") for event in events)
        assert any(event["event_type"] == "assistant_message_completed" for event in events)
        assert not any(event["event_type"] == "error" for event in events)
        assert provider.requests[1].observations[0].error.startswith("Unknown agent tool")
        session_factory = get_session_factory()
        with session_factory() as session:
            assert session.scalar(select(ToolInvocation)) is None


def test_invalid_tool_call_is_returned_to_model_as_observation(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "invalid-tool-observation.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("network.open_socket", {"reason": "scan"}),
                ModelFinalAnswer("I cannot use that unavailable low-level network tool."),
            ]
        ),
    )

    with TestClient(app) as client:
        events = _send_and_stream(client, "Please scan the network.")

        assert any(event["event_type"] == "model_action_validation" and event["payload"]["valid"] is False for event in events)
        assert any(event["event_type"] == "model_observation" and event["payload"].get("error") for event in events)
        assert any(event["event_type"] == "assistant_message_completed" for event in events)
        assert not any(event["event_type"] == "error" for event in events)
        assert provider.requests[1].observations[0].error.startswith("Unknown agent tool")


def test_discovery_tool_is_available_for_model_operated_network_scan(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "discovery-tool.db")

    def fake_inspect_home_network(session):
        return {
            "run": {"id": "run-test"},
            "entity_refs": ["device:dev-found"],
            "candidate_count": 1,
            "integrated_devices": 1,
            "new_device_ids": ["dev-found"],
            "result": "candidates_found",
            "scope": {"scan_subnets": ["198.51.100.0/24"]},
            "source_results": [
                {
                    "source_name": "local_network_live",
                    "status": "completed",
                    "message": "completed: 1 local HTTP candidate from 1 configured subnet scan.",
                    "candidate_count": 1,
                }
            ],
        }

    monkeypatch.setattr("app.agent.tools.discovery.inspect_home_network", fake_inspect_home_network)
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("discovery.inspect_home_network", {"reason": "user asked to scan"}),
                ModelFinalAnswer("I ran discovery and found one candidate."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(
            device_id="dev-found",
            name="Found HTTP Endpoint",
            device_type="smart_appliance",
            manufacturer="Test",
            protocols=["http_local"],
        )
        events = _send_and_stream(client, "Please scan the network.")

        runtime_started = next(event for event in events if event["event_type"] == "agent_runtime_started")
        assert "discovery.inspect_home_network" in runtime_started["payload"]["available_tools"]
        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        assert tool_finished["payload"]["tool_name"] == "discovery.inspect_home_network"
        assert tool_finished["payload"]["result"]["candidate_count"] == 1
        assert tool_finished["payload"]["result"]["canonical_device_count"] == 1
        assert tool_finished["payload"]["result"]["observed_class_counts"] == {"local_http_endpoint": 1}
        assert tool_finished["payload"]["result"]["role_hypothesis_counts"] == {}
        assert tool_finished["payload"]["result"]["primary_observations"][0]["classification_status"] == "unclassified"
        assert tool_finished["payload"]["result"]["refs"]["entity_refs"] == ["device:dev-found"]
        assert "new_device_ids" not in tool_finished["payload"]["result"]


def test_discovery_tool_failure_marks_invocation_task_and_step_failed(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "discovery-tool-failure.db")

    def failing_inspect_home_network(session):
        session.execute(text("insert into devices (id) values ('dev-invalid')"))

    monkeypatch.setattr("app.agent.tools.discovery.inspect_home_network", failing_inspect_home_network)
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("discovery.inspect_home_network", {"reason": "user asked to scan"}),
                ModelFinalAnswer("Discovery failed; I will explain the recorded error."),
            ]
        ),
    )

    with TestClient(app) as client:
        events = _send_and_stream(client, "Please scan the network.")

        assert any(event["event_type"] == "tool_failed" for event in events)
        assert any(event["event_type"] == "assistant_message_completed" for event in events)

        session_factory = get_session_factory()
        with session_factory() as session:
            invocation = session.scalar(select(ToolInvocation).where(ToolInvocation.tool_name == "discovery.inspect_home_network"))
            assert invocation is not None
            assert invocation.status == "failed"
            assert invocation.finished_at is not None
            assert "devices" in invocation.error

            task = session.scalar(select(AgentTask).where(AgentTask.task_type == "discover_home"))
            assert task is not None
            assert task.status == "failed"
            assert task.completed_at is not None
            assert task.context["failure_summary"] == "discovery_failed"

            step = session.scalar(select(TaskStep).where(TaskStep.task_id == task.id, TaskStep.step_key == "run_discovery"))
            assert step is not None
            assert step.status == "failed"
            assert step.result["error_type"]

            blocker = session.scalar(select(Blocker).where(Blocker.task_id == task.id))
            assert blocker is not None
            assert blocker.status == "open"


def test_user_decision_route_is_the_only_approval_path_for_role_proposals(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "decision-route.db")
    _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall(
                    "role.prepare_binding_proposal",
                    {
                        "entity_ref": "device:dev-ppc",
                        "role": "grid_meter",
                        "label": "PPC SMGW",
                        "rationale": "User asked for proposal.",
                    },
                ),
                ModelFinalAnswer("Please decide on the proposal."),
            ]
        ),
    )

    with TestClient(app) as client:
        client.get("/api/v1/agent/thread")
        _add_device(device_id="dev-ppc", name="PPC SMGW", device_type="grid_meter", manufacturer="PPC")
        events = _send_and_stream(client, "PPC SMGW vormerken.")
        decision_request_id = next(event for event in events if event["event_type"] == "decision_request_created")["payload"]["decision_request_id"]

        missing_route = client.post("/api/v1/agent/proposals/proposal-x/confirm")
        assert missing_route.status_code == 404

        response = client.post(f"/api/v1/agent/decision-requests/{decision_request_id}/responses", json={"decision": "approve"})
        assert response.status_code == 200
        session_factory = get_session_factory()
        with session_factory() as session:
            role_candidate = session.scalar(select(HomeGraphEntity).where(HomeGraphEntity.entity_type == "role_candidate"))
            assert role_candidate is not None
            assert role_candidate.status == "accepted"
            assert session.scalar(select(HemsSystemBinding)) is None


def test_provider_not_ready_produces_traceable_error_without_stub_reply(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "provider-error.db")

    with TestClient(app) as client:
        events = _send_and_stream(client, "hello")
        assert any(event["event_type"] == "provider_error" for event in events)
        assert any(event["event_type"] == "error" for event in events)
        assert not any(event["event_type"] == "assistant_message_completed" for event in events)


def test_runtime_max_iterations_requests_forced_final_answer(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "max-iterations.db")
    provider = _use_scripted_provider(
        monkeypatch,
        ScriptedModelProvider(
            [
                ModelToolCall("work.get_status", {}),
                ModelToolCall("work.get_status", {}),
                ModelToolCall("work.get_status", {}),
                ModelToolCall("work.get_status", {}),
                ModelToolCall("work.get_status", {}),
                ModelToolCall("work.get_status", {}),
                ModelFinalAnswer("I reached the tool limit and summarized the current work state."),
            ]
        ),
    )

    with TestClient(app) as client:
        events = _send_and_stream(client, "Keep checking status.")
        assert len([event for event in events if event["event_type"] == "tool_finished"]) == 6
        assert any(event["event_type"] == "runtime_warning" for event in events)
        completed = next(event for event in events if event["event_type"] == "assistant_message_completed")
        assert "tool limit" in completed["payload"]["message"]["content"]
        assert provider.requests[-1].force_final is True
