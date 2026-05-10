from __future__ import annotations

from app.actions.service import ActionContext, execute_action
from app.agent.tools.schemas import (
    AgentToolContext,
    ToolExecutionResult,
    UiFocusEntitiesInput,
    UiOpenConnectionOverlayInput,
    UiOpenDeviceDetailsInput,
    focus_entities_event,
)


class UiFocusEntitiesTool:
    name = "ui.focus_entities"
    purpose = "Focuses or highlights entities in the shared workspace, so the user can see which devices, roles, tasks, or blockers you are referring to."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = UiFocusEntitiesInput
    mutates_state = False
    reads: list[str] = []
    writes: list[str] = []
    side_effects = ["emits typed UI focus event"]
    emitted_ui_events = ["entity.focus"]

    def execute(self, context: AgentToolContext, payload: UiFocusEntitiesInput) -> ToolExecutionResult:
        return ToolExecutionResult(ui_events=[focus_entities_event(payload.entity_refs, payload.reason, payload.mode)])


class UiOpenDeviceDetailsTool:
    name = "ui.open_device_details"
    purpose = "Opens the shared workspace detail view for one device or entity."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = UiOpenDeviceDetailsInput
    mutates_state = False
    reads: list[str] = []
    writes: list[str] = []
    side_effects = ["emits typed UI device detail event"]
    emitted_ui_events = ["device.details.open"]

    def execute(self, context: AgentToolContext, payload: UiOpenDeviceDetailsInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "ui.open_device_details",
            payload.model_dump(),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)


class UiOpenConnectionOverlayTool:
    name = "ui.open_connection_overlay"
    purpose = "Opens the shared workspace connection overlay for a selected entity, protocol endpoint, and integration path."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = UiOpenConnectionOverlayInput
    mutates_state = False
    reads = ["home_graph", "protocol_endpoints", "work_store"]
    writes: list[str] = []
    side_effects = ["emits typed UI connection overlay event"]
    emitted_ui_events = ["connection.overlay.open"]

    def execute(self, context: AgentToolContext, payload: UiOpenConnectionOverlayInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "ui.open_connection_overlay",
            payload.model_dump(),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)
