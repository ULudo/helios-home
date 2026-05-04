from __future__ import annotations

from app.agent.tools.schemas import (
    AgentToolContext,
    DeviceAssessInput,
    ToolExecutionResult,
    focus_entities_event,
    show_assessment_event,
    view_open_event,
)
from app.workflows.device_assessment import assess_home_graph_entity


class DeviceAssessTool:
    name = "device.assess"
    purpose = "Assess what a Home Graph candidate or device likely is and which HEMS role it may serve."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "debug")
    input_model = DeviceAssessInput
    mutates_state = True
    reads = ["home_graph", "home_graph_evidence"]
    writes = ["device_assessment", "home_graph_evidence"]
    side_effects = ["records tentative device assessment evidence"]
    emitted_ui_events = ["view.open", "entity.focus", "assessment.show"]

    def execute(self, context: AgentToolContext, payload: DeviceAssessInput) -> ToolExecutionResult:
        result = assess_home_graph_entity(
            context.session,
            entity_ref=payload.entity_ref,
            question=payload.question or context.user_message,
        )
        return ToolExecutionResult(
            output=result,
            ui_events=[
                view_open_event("overview", "focus"),
                focus_entities_event([payload.entity_ref]),
                focus_entities_event([payload.entity_ref], mode="highlight"),
                show_assessment_event(result["assessment_ref"], payload.entity_ref),
            ],
        )
