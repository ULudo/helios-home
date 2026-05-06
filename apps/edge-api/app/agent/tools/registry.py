from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from app.agent.tools.device import DeviceAssessTool
from app.agent.tools.discovery import DiscoveryInspectHomeNetworkTool
from app.agent.tools.evidence import EvidenceRecordUserAssertionTool
from app.agent.tools.home_graph import HomeGraphGetEntityDetailsTool, HomeGraphQueryTool
from app.agent.tools.reference import HomeGraphResolveEntityReferenceTool
from app.agent.tools.roles import RolePrepareBindingProposalTool
from app.agent.tools.schemas import AgentTool, AgentToolContext, ToolExecutionResult, ToolSpecRead
from app.agent.tools.ui import UiFocusEntitiesTool
from app.agent.tools.work import WorkGetStatusTool
from app.db.models import AgentUiEvent, AuditEvent, ToolInvocation, utcnow


class ToolRegistry:
    def __init__(self, tools: list[AgentTool]):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def available_tools(self, mode: str) -> list[AgentTool]:
        return [tool for tool in self._tools.values() if mode in tool.contexts]

    def specs_for_mode(self, mode: str) -> list[ToolSpecRead]:
        specs: list[ToolSpecRead] = []
        for tool in self.available_tools(mode):
            schema = tool.input_model.model_json_schema()
            specs.append(
                ToolSpecRead(
                    name=tool.name,
                    purpose=tool.purpose,
                    risk_level=tool.risk_level,
                    confirmation_policy=tool.confirmation_policy,
                    contexts=list(tool.contexts),
                    input_schema=schema,
                    output_schema=getattr(tool, "output_schema", {}),
                    mutates_state=bool(getattr(tool, "mutates_state", False)),
                    reads=list(getattr(tool, "reads", [])),
                    writes=list(getattr(tool, "writes", [])),
                    side_effects=list(getattr(tool, "side_effects", [])),
                    emitted_ui_events=list(getattr(tool, "emitted_ui_events", [])),
                    executor=f"{tool.__class__.__module__}.{tool.__class__.__name__}",
                )
            )
        return specs


def create_default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            HomeGraphQueryTool(),
            HomeGraphGetEntityDetailsTool(),
            HomeGraphResolveEntityReferenceTool(),
            EvidenceRecordUserAssertionTool(),
            DiscoveryInspectHomeNetworkTool(),
            DeviceAssessTool(),
            RolePrepareBindingProposalTool(),
            WorkGetStatusTool(),
            UiFocusEntitiesTool(),
        ]
    )


def execute_registered_tool(
    registry: ToolRegistry,
    context: AgentToolContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, ToolExecutionResult]:
    tool = registry.get(tool_name)
    if tool is None:
        raise ValueError(f"Unknown agent tool: {tool_name}")
    if context.mode not in tool.contexts:
        raise PermissionError(f"Tool {tool_name} is not available in {context.mode} context.")

    payload: BaseModel = tool.input_model.model_validate(arguments)
    invocation = ToolInvocation(
        id=f"tool-{uuid4().hex[:12]}",
        site_id=context.site.id,
        thread_id=context.thread.id,
        turn_id=context.turn.id,
        tool_name=tool.name,
        risk_level=tool.risk_level,
        confirmation_policy=tool.confirmation_policy,
        status="running",
        input_payload=jsonable_encoder(payload.model_dump()),
        started_at=utcnow(),
    )
    context.session.add(invocation)
    context.session.add(
        AuditEvent(
            actor="agent",
            action="start_tool_invocation",
            target_type="tool_invocation",
            target_id=invocation.id,
            summary=f"Started {tool.name}.",
            details={
                "tool_name": tool.name,
                "risk_level": tool.risk_level,
                "confirmation_policy": tool.confirmation_policy,
            },
            created_at=utcnow(),
        )
    )
    context.session.commit()

    try:
        result = tool.execute(context, payload)
    except Exception as exc:
        invocation.status = "failed"
        invocation.error = str(exc)
        invocation.finished_at = utcnow()
        context.session.add(invocation)
        context.session.add(
            AuditEvent(
                actor="agent",
                action="fail_tool_invocation",
                target_type="tool_invocation",
                target_id=invocation.id,
                summary=f"{tool.name} failed.",
                details={"error": str(exc)},
                created_at=utcnow(),
            )
        )
        context.session.commit()
        raise

    invocation.status = "completed"
    invocation.output_payload = jsonable_encoder(result.model_dump())
    invocation.finished_at = utcnow()
    context.session.add(invocation)
    for ui_event in result.ui_events:
        context.session.add(
            AgentUiEvent(
                id=f"ui-{uuid4().hex[:12]}",
                site_id=context.site.id,
                thread_id=context.thread.id,
                turn_id=context.turn.id,
                event_type=ui_event.event_type,
                payload=jsonable_encoder(ui_event.payload),
                created_at=utcnow(),
            )
        )
    context.session.add(
        AuditEvent(
            actor="agent",
            action="complete_tool_invocation",
            target_type="tool_invocation",
            target_id=invocation.id,
            summary=f"Completed {tool.name}.",
            details={
                "tool_name": tool.name,
                "created_proposal_refs": result.created_proposal_refs,
                "created_decision_request_refs": result.created_decision_request_refs,
            },
            created_at=utcnow(),
        )
    )
    context.session.commit()
    return invocation.id, result
