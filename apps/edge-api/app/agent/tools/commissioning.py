from __future__ import annotations

from sqlalchemy import select

from app.agent.tools.schemas import AgentToolContext, CommissioningGetLogInput, ToolExecutionResult
from app.db.models import ProtocolDiagnosticRun
from app.home_graph.service import resolve_entity


class CommissioningGetLogTool:
    name = "commissioning.get_log"
    purpose = "Read compact commissioning and protocol diagnostic logs for one entity or explicit diagnostic runs."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "debug")
    input_model = CommissioningGetLogInput
    mutates_state = False
    reads = ["protocol_diagnostic_run", "work_store"]
    writes: list[str] = []
    side_effects: list[str] = []
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: CommissioningGetLogInput) -> ToolExecutionResult:
        statement = select(ProtocolDiagnosticRun).where(ProtocolDiagnosticRun.site_id == context.site.id)
        if payload.diagnostic_run_refs:
            statement = statement.where(ProtocolDiagnosticRun.id.in_(payload.diagnostic_run_refs))
        elif payload.entity_ref:
            entity = resolve_entity(context.session, payload.entity_ref)
            if entity is None:
                raise ValueError(f"Unknown Home Graph entity: {payload.entity_ref}")
            statement = statement.where(ProtocolDiagnosticRun.entity_ref == entity.id)
        runs = context.session.scalars(
            statement.order_by(ProtocolDiagnosticRun.created_at.desc()).limit(payload.limit)
        ).all()
        return ToolExecutionResult(
            output={
                "diagnostic_runs": [
                    {
                        "diagnostic_run_ref": run.id,
                        "entity_ref": run.entity_ref,
                        "endpoint_ref": run.endpoint_ref,
                        "protocol": run.protocol,
                        "integration_path": run.integration_path,
                        "status": run.status,
                        "result": run.result or {},
                        "log_entries": run.log_entries or [],
                        "created_at": run.created_at,
                    }
                    for run in runs
                ]
            }
        )
