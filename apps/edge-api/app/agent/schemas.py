from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.schemas import DebugCaseRead


class SetupSystemBindingRead(BaseModel):
    system_type: str
    label: str
    device_id: str | None = None
    asset_id: str | None = None
    device_name: str | None = None
    status: str = "confirmed"
    connection_status: str = "unknown"
    telemetry_status: str = "unknown"
    control_status: str = "unknown"


class SetupItemRead(BaseModel):
    kind: str
    label: str
    details: str = ""
    status: str = "open"


class SiteSetupProfileRead(BaseModel):
    summary: str
    confirmed_systems: list[SetupSystemBindingRead] = Field(default_factory=list)
    unresolved_items: list[SetupItemRead] = Field(default_factory=list)
    user_notes: list[str] = Field(default_factory=list)
    updated_at: datetime


class AgentMessageRead(BaseModel):
    id: str
    role: str
    content: str
    status: str
    created_at: datetime
    turn_id: str | None = None


class ActionProposalRead(BaseModel):
    id: str
    action_type: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    title: str = ""
    risk_level: str = "medium"
    target_refs: list[str] = Field(default_factory=list)
    decision_request_id: str | None = None
    decision_question: str | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None


class AgentBlockerRead(BaseModel):
    id: str
    task_id: str | None = None
    subject_ref: str = ""
    blocker_type: str
    summary: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    resolved_at: datetime | None = None


class AgentTaskRead(BaseModel):
    id: str
    task_type: str
    title: str
    goal: str
    status: str
    target_refs: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    blockers: list[AgentBlockerRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class AgentThreadRead(BaseModel):
    id: str
    title: str
    status: str
    messages: list[AgentMessageRead] = Field(default_factory=list)
    pending_proposals: list[ActionProposalRead] = Field(default_factory=list)
    active_tasks: list[AgentTaskRead] = Field(default_factory=list)
    open_blockers: list[AgentBlockerRead] = Field(default_factory=list)
    setup_profile: SiteSetupProfileRead
    latest_debug_case: DebugCaseRead | None = None
    created_at: datetime
    updated_at: datetime


class AgentMessageCreate(BaseModel):
    content: str
    context: dict[str, Any] = Field(default_factory=dict)


class AgentTurnAcceptedRead(BaseModel):
    thread_id: str
    turn_id: str
    user_message: AgentMessageRead


class AgentTurnEventRead(BaseModel):
    turn_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ActionProposalDecisionRead(BaseModel):
    proposal: ActionProposalRead
    thread: AgentThreadRead


class UserDecisionCreate(BaseModel):
    decision: str
    comment: str = ""


class AgentProviderOptionRead(BaseModel):
    provider_id: str
    label: str
    description: str
    auth_kind: str
    base_url_default: str | None = None
    model_placeholder: str
    supports_base_url: bool = True
    supports_model: bool = True
    selected: bool = False
    model: str = ""
    base_url: str | None = None
    api_key_configured: bool = False
    ready: bool = False


class AgentProviderConfigRead(BaseModel):
    selected_provider: str
    effective_provider: str
    ready: bool
    message: str
    provider_options: list[AgentProviderOptionRead] = Field(default_factory=list)


class AgentProviderConfigUpdate(BaseModel):
    provider_id: str
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    clear_api_key: bool = False
