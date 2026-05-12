from __future__ import annotations

from app.actions.service import ActionContext, execute_action
from app.agent.tools.schemas import AgentToolContext, LoadControlConfigureDeviceInput, ToolExecutionResult


class LoadControlConfigureDeviceTool:
    name = "load_control.configure_device"
    purpose = (
        "Configures whether a device receives LPC/LPP constraints and whether it participates in LPC/LPP distribution."
    )
    risk_level = "medium"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "operation", "debug")
    input_model = LoadControlConfigureDeviceInput
    mutates_state = True
    reads = ["inventory", "hems_load_control_config"]
    writes = ["hems_load_control_config", "audit_log"]
    side_effects = ["changes future HEMS load-control distribution behavior"]
    emitted_ui_events = ["device.details.open"]

    def execute(self, context: AgentToolContext, payload: LoadControlConfigureDeviceInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "load_control.configure_device",
            payload.model_dump(exclude_none=True),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)
