from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text

from app.core.config import get_settings
from app.db.models import (
    AgentTask,
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
)
from app.db.session import get_engine, get_session_factory
from app.home_graph.service import sync_inventory_to_home_graph
from app.main import create_app
from app.agent.provider import (
    ModelFinalAnswer,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    _model_action_from_openai_responses_payload,
    _provider_tool_name_map,
)


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
        assert "eebus.identity.get_or_create" in runtime_started["payload"]["available_tools"]
        assert "commissioning.start_or_continue" in runtime_started["payload"]["available_tools"]
        assert "commissioning.get_log" in runtime_started["payload"]["available_tools"]
        assert "discovery.inspect_home_network" in runtime_started["payload"]["available_tools"]
        assert "ui.focus_entities" in runtime_started["payload"]["available_tools"]
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
        assert context["home_inventory"]["canonical_device_count"] == 25
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
        assert result["connection_facets"]["overall_connection_state"] == "endpoint_visible"
        assert result["connection_facets"]["facets"]["endpoint_state"] == "visible"
        assert "next_step" not in result["entity"]["properties"]
        assert "explanation" not in result["entity"]["properties"]
        assert result["protocol_endpoints"][0]["protocol"] == "eebus_ship"
        assert result["protocol_endpoints"][0]["port"] == 4711
        assert "recommended_option" not in result


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
        assert "remote_auto_registration_closed" in blocker_codes
        assert "ship_trust_commissioning_not_validated" in blocker_codes
        facts = result["inspections"][0]["facts"]
        assert facts["remote_register"] is False
        assert facts["local_identity_exists"] is False
        assert any(transition["tool"] == "eebus.identity.get_or_create" for transition in result["available_transitions"])
        session_factory = get_session_factory()
        with session_factory() as session:
            diagnostic = session.get(ProtocolDiagnosticRun, result["diagnostic_run_ref"])
            assert diagnostic is not None
            assert diagnostic.status == "blocked"
            assert diagnostic.result["blocker_codes"]


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
        assert "private_key_pem" not in identity
        assert tool_finished["payload"]["result"]["private_key_exported"] is False


def test_commissioning_start_or_continue_prepares_eebus_identity_and_records_trust_blocker(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-start.db")

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
                        "commissioning.start_or_continue",
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
        assert tool_finished["payload"]["tool_name"] == "commissioning.start_or_continue"
        assert result["status"] == "blocked_waiting_for_user_action"
        assert result["phase"] == "waiting_for_peer_trust"
        assert len(result["local_identity"]["ski"]) == 40
        assert result["required_external_action"]["action"] == "authorize_local_ski_on_peer"
        assert result["connection_facets"]["overall_connection_state"] == "blocked"
        assert "no_spine_feature_validation" in result["effects_not_included"]

        session_factory = get_session_factory()
        with session_factory() as session:
            diagnostic = session.get(ProtocolDiagnosticRun, result["diagnostic_run_ref"])
            assert diagnostic is not None
            assert diagnostic.result["blocker_codes"] == ["eebus_peer_trust_required"]
            blocker = session.scalar(select(Blocker).where(Blocker.blocker_type == "eebus_peer_trust_required"))
            assert blocker is not None
            assert blocker.status == "open"


def test_commissioning_get_log_reads_compact_diagnostic_entries(tmp_path, monkeypatch):
    app = _bootstrap_app(tmp_path, monkeypatch, "commissioning-log.db")

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
                        "commissioning.start_or_continue",
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
        assert any(entry["event"] == "manual_peer_trust_required" for entry in log_result["diagnostic_runs"][0]["log_entries"])


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
        events = _send_and_stream(client, "Please scan the network.")

        runtime_started = next(event for event in events if event["event_type"] == "agent_runtime_started")
        assert "discovery.inspect_home_network" in runtime_started["payload"]["available_tools"]
        tool_finished = next(event for event in events if event["event_type"] == "tool_finished")
        assert tool_finished["payload"]["tool_name"] == "discovery.inspect_home_network"
        assert tool_finished["payload"]["result"]["candidate_count"] == 1


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
