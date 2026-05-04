from __future__ import annotations

from app.agent.tools.schemas import (
    AgentToolContext,
    ToolExecutionResult,
    UiFocusEntitiesInput,
    focus_entities_event,
)


class UiFocusEntitiesTool:
    name = "ui.focus_entities"
    purpose = "Focus or highlight specific Home Graph entities in the UI."
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
