from __future__ import annotations

from app.actions.service import ActionContext, execute_action
from app.agent.tools.schemas import AgentToolContext, InventoryRemoveDeviceInput, ToolExecutionResult


class InventoryRemoveDeviceTool:
    name = "inventory.remove_device"
    purpose = "Removes a selected device from the current HEMS inventory. A later discovery can add it again if it is still found."
    risk_level = "medium"
    confirmation_policy = "user_message_required"
    contexts = ("setup", "operation", "debug")
    input_model = InventoryRemoveDeviceInput
    mutates_state = True
    reads = ["inventory", "home_graph", "work_store"]
    writes = ["inventory", "home_graph", "work_store", "audit_log"]
    side_effects = ["may stop active protocol runtime sessions for the removed endpoint"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: InventoryRemoveDeviceInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "inventory.remove_device",
            payload.model_dump(),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)
