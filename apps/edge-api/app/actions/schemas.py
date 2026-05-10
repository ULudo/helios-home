from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ActionExecuteRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ActionExecutionRead(BaseModel):
    action_name: str
    actor: Literal["user", "agent", "system"]
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    ui_events: list[dict[str, Any]] = Field(default_factory=list)


class ConnectionActionRef(BaseModel):
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ConnectionEndpointOptionRead(BaseModel):
    endpoint_ref: str
    owner_ref: str
    protocol: str
    host: str
    port: int | None = None
    service_name: str
    status: str
    source: str
    last_seen_at: str
    confidence: float
    allowed_integration_paths: list[str]
    connectable: bool
    state: dict[str, Any] = Field(default_factory=dict)
    connect_action: ConnectionActionRef | None = None


class ConnectionOptionsRead(BaseModel):
    entity_ref: str
    device_id: str
    display_name: str
    endpoints: list[ConnectionEndpointOptionRead] = Field(default_factory=list)


class ConnectionStateRead(BaseModel):
    entity_ref: str
    endpoint_ref: str
    protocol: str = ""
    host: str = ""
    port: int | None = None
    service_name: str = ""
    integration_path: str
    phase: str
    status: str
    can_connect: bool
    steps: list[dict[str, Any]] = Field(default_factory=list)
    required_user_action: dict[str, Any] = Field(default_factory=dict)
    connection_facets: dict[str, Any] = Field(default_factory=dict)
    diagnostic_run_ref: str = ""
    task_ref: str = ""
    local_ski: str = ""
    peer_ski: str = ""
    last_error: str = ""
    updated_at: datetime | None = None
    connect_action: ConnectionActionRef | None = None
