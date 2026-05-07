from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import ConversationThread, ConversationTurn, Site


RiskLevel = Literal["low", "medium", "high"]
ConfirmationPolicy = Literal["none", "user_decision_required", "user_message_required", "developer_mode_only"]
ToolContextMode = Literal["conversation", "setup", "commissioning", "operation", "debug"]
HemsRole = Literal["grid_meter", "ev_charger", "pv_inverter", "battery", "heat_pump", "controllable_load"]
IntegrationPath = Literal["eebus_spine", "modbus_tcp", "sunspec_modbus", "http_local", "mqtt", "vendor_cloud"]
UiEventType = Literal[
    "view.open",
    "entity.focus",
    "entity.relationship.show",
    "task.show",
    "proposal.present",
    "evidence.recorded",
    "assessment.show",
]


class ToolSpecRead(BaseModel):
    name: str
    purpose: str
    risk_level: RiskLevel
    confirmation_policy: ConfirmationPolicy
    contexts: list[ToolContextMode]
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    mutates_state: bool = False
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    emitted_ui_events: list[UiEventType] = Field(default_factory=list)
    executor: str = ""


class AgentUiEvent(BaseModel):
    event_type: UiEventType
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    output: dict[str, Any] = Field(default_factory=dict)
    ui_events: list[AgentUiEvent] = Field(default_factory=list)
    created_proposal_refs: list[str] = Field(default_factory=list)
    created_decision_request_refs: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class AgentToolContext:
    session: Session
    site: Site
    thread: ConversationThread
    turn: ConversationTurn
    user_message: str
    input_context: dict[str, Any] = field(default_factory=dict)
    mode: ToolContextMode = "setup"


class AgentTool(Protocol):
    name: str
    purpose: str
    risk_level: RiskLevel
    confirmation_policy: ConfirmationPolicy
    contexts: tuple[ToolContextMode, ...]
    input_model: type[BaseModel]

    def execute(self, context: AgentToolContext, payload: BaseModel) -> ToolExecutionResult:
        ...


class HomeGraphQueryInput(BaseModel):
    entity_refs: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    scope: Literal["canonical_devices", "raw_artifacts", "all"] = "canonical_devices"
    role_hypothesis: HemsRole | None = None
    text: str = ""
    include_evidence: bool = False
    include_relationships: bool = True


class HomeGraphResolveEntityReferenceInput(BaseModel):
    text: str
    role: HemsRole | None = None
    candidate_refs: list[str] = Field(default_factory=list)
    max_results: int = Field(default=5, ge=1, le=12)


class HomeGraphGetEntityDetailsInput(BaseModel):
    entity_ref: str
    include_evidence: bool = True


class EvidenceRecordUserAssertionInput(BaseModel):
    subject_ref: str
    assertion_type: Literal["identity", "role_hint", "location", "alias", "correction"]
    value: str
    source_turn_ref: str | None = None


class DiscoveryInspectHomeNetworkInput(BaseModel):
    reason: str = ""


class DiscoveryInspectKnownEndpointInput(BaseModel):
    host: str
    reason: str = ""


class DeviceAssessInput(BaseModel):
    entity_ref: str
    question: str = ""


class RolePrepareBindingProposalInput(BaseModel):
    entity_ref: str
    role: HemsRole
    endpoint_ref: str = ""
    integration_path: IntegrationPath | Literal[""] = ""
    label: str = ""
    rationale: str = ""


class ProtocolListEndpointsInput(BaseModel):
    entity_ref: str = ""
    protocol: str = ""


class ConnectionInspectReadinessInput(BaseModel):
    entity_ref: str = ""
    endpoint_ref: str = ""
    integration_path: IntegrationPath | Literal[""] = ""
    role: HemsRole | None = None


class EebusIdentityGetOrCreateInput(BaseModel):
    common_name: str = "Helios Home HEMS"


class CommissioningStartOrContinueInput(BaseModel):
    entity_ref: str
    endpoint_ref: str = ""
    integration_path: IntegrationPath | Literal[""] = ""
    role: HemsRole | None = None
    reason: str = ""


class CommissioningGetLogInput(BaseModel):
    entity_ref: str = ""
    diagnostic_run_refs: list[str] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=20)


class WorkGetStatusInput(BaseModel):
    task_refs: list[str] = Field(default_factory=list)
    include_steps: bool = True
    include_blockers: bool = True


class UiFocusEntitiesInput(BaseModel):
    entity_refs: list[str]
    mode: Literal["focus", "highlight"] = "focus"
    reason: str = ""


def view_open_event(view: Literal["overview", "settings"], mode: Literal["peek", "focus", "switch"] = "focus") -> AgentUiEvent:
    return AgentUiEvent(event_type="view.open", payload={"view": view, "mode": mode})


def focus_entities_event(entity_refs: list[str], reason: str = "", mode: Literal["focus", "highlight"] = "focus") -> AgentUiEvent:
    return AgentUiEvent(event_type="entity.focus", payload={"entity_refs": entity_refs, "reason": reason, "mode": mode})


def show_relationship_event(from_ref: str, to_ref: str, relationship: str) -> AgentUiEvent:
    return AgentUiEvent(
        event_type="entity.relationship.show",
        payload={"from_ref": from_ref, "to_ref": to_ref, "relationship": relationship},
    )


def show_task_event(
    task_ref: str,
    mode: Literal["progress", "blockers", "summary"] = "summary",
    *,
    title: str = "",
    status: str = "",
    summary: str = "",
    blockers: list[dict[str, Any]] | None = None,
) -> AgentUiEvent:
    payload: dict[str, Any] = {"task_ref": task_ref, "mode": mode}
    if title:
        payload["title"] = title
    if status:
        payload["status"] = status
    if summary:
        payload["summary"] = summary
    if blockers:
        payload["blockers"] = blockers
    return AgentUiEvent(event_type="task.show", payload=payload)


def present_proposal_event(proposal_ref: str, decision_request_ref: str) -> AgentUiEvent:
    return AgentUiEvent(
        event_type="proposal.present",
        payload={"proposal_ref": proposal_ref, "decision_request_ref": decision_request_ref},
    )


def show_assessment_event(assessment_ref: str, entity_ref: str) -> AgentUiEvent:
    return AgentUiEvent(event_type="assessment.show", payload={"assessment_ref": assessment_ref, "entity_ref": entity_ref})
