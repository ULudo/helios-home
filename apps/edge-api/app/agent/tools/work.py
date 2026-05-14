from __future__ import annotations

from sqlalchemy import select

from app.agent.tools.schemas import AgentToolContext, ToolExecutionResult, WorkGetStatusInput
from app.db.models import AgentTask, Blocker, UserDecisionRequest


class WorkGetStatusTool:
    name = "work.get_status"
    purpose = "Inspect ongoing HEMS-management tasks, blockers, and pending user decisions."
    risk_level = "low"
    confirmation_policy = "none"
    contexts = ("conversation", "setup", "commissioning", "operation", "debug")
    input_model = WorkGetStatusInput
    mutates_state = False
    reads = ["work_store", "user_decision_requests"]
    writes: list[str] = []
    side_effects: list[str] = []
    emitted_ui_events: list[str] = []

    def execute(self, context: AgentToolContext, payload: WorkGetStatusInput) -> ToolExecutionResult:
        task_statement = select(AgentTask).where(AgentTask.site_id == context.site.id).order_by(AgentTask.updated_at.desc())
        if payload.task_refs:
            task_statement = task_statement.where(AgentTask.id.in_(payload.task_refs))
        tasks = context.session.scalars(task_statement.limit(12)).all()
        blockers = context.session.scalars(
            select(Blocker)
            .where(Blocker.status == "open")
            .order_by(Blocker.created_at.desc())
            .limit(12)
        ).all()
        decisions = context.session.scalars(
            select(UserDecisionRequest)
            .where(UserDecisionRequest.thread_id == context.thread.id, UserDecisionRequest.status == "pending")
            .order_by(UserDecisionRequest.created_at.desc())
        ).all()
        serialized_tasks = [
            {
                "task_ref": task.id,
                "task_type": task.task_type,
                "title": task.title,
                "goal": task.goal,
                "status": task.status,
                "target_refs": task.target_refs or [],
            }
            for task in tasks
        ]
        serialized_blockers = [
            {
                "blocker_ref": blocker.id,
                "task_ref": blocker.task_id,
                "subject_ref": blocker.subject_ref,
                "summary": blocker.summary,
            }
            for blocker in blockers
        ] if payload.include_blockers else []
        return ToolExecutionResult(
            output={
                "tasks": serialized_tasks,
                "active_blockers": serialized_blockers,
                "pending_decisions": [
                    {
                        "decision_request_ref": decision.id,
                        "proposal_ref": decision.proposal_id,
                        "question": decision.question,
                        "risk_level": decision.risk_level,
                    }
                    for decision in decisions
                ],
            },
        )
