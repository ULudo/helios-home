from __future__ import annotations

from app.actions.service import ActionContext, execute_action
from app.agent.tools.schemas import AgentToolContext, ConnectionDisconnectInput, ConnectionEstablishInput, ToolExecutionResult


class ConnectionEstablishTool:
    name = "connection.establish"
    purpose = (
        "Establish a connection for a chosen entity, endpoint, and integration path. "
        "The model chooses what should be connected; Helios performs the deterministic protocol workflow."
    )
    risk_level = "medium"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "debug")
    input_model = ConnectionEstablishInput
    mutates_state = True
    reads = ["home_graph", "protocol_endpoints", "eebus_local_identity", "work_store"]
    writes = ["eebus_local_identity", "work_store", "protocol_diagnostic_run"]
    side_effects = ["may open network connections to the selected endpoint and may create a local protocol identity"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: ConnectionEstablishInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "connection.establish",
            payload.model_dump(),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)


class ConnectionDisconnectTool:
    name = "connection.disconnect"
    purpose = (
        "Disconnects a selected HEMS connection for an entity, endpoint, and integration path. "
        "The model chooses the connection; Helios performs the deterministic teardown."
    )
    risk_level = "medium"
    confirmation_policy = "none"
    contexts = ("setup", "commissioning", "debug")
    input_model = ConnectionDisconnectInput
    mutates_state = True
    reads = ["home_graph", "protocol_endpoints", "eebus_runtime", "work_store"]
    writes = ["protocol_endpoints", "protocol_diagnostic_run"]
    side_effects = ["may close local protocol sessions and marks the selected endpoint as disconnected"]
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: ConnectionDisconnectInput) -> ToolExecutionResult:
        result = execute_action(
            ActionContext(
                session=context.session,
                site=context.site,
                actor="agent",
                thread_id=context.thread.id,
                turn_id=context.turn.id,
            ),
            "connection.disconnect",
            payload.model_dump(),
        )
        return ToolExecutionResult(output=result.output, ui_events=result.ui_events)
