from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.provider import (
    ModelFinalAnswer,
    ModelObservation,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ProviderError,
)
from app.agent.schemas import AgentTurnEventRead
from app.agent.tools.registry import ToolRegistry, execute_registered_tool
from app.agent.tools.schemas import AgentToolContext
from app.db.models import ConversationMessage, ConversationThread, ConversationTurn, Proposal, Site, UserDecisionRequest


ContextBuilder = Callable[[Session, ConversationThread, dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
EventWriter = Callable[[Session, ConversationTurn, str, dict[str, Any]], AgentTurnEventRead]
ProposalSerializer = Callable[[Proposal, UserDecisionRequest | None], Any]


@dataclass(slots=True)
class RuntimeResult:
    final_answer: str
    events: list[AgentTurnEventRead] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class AgentRuntime:
    def __init__(
        self,
        *,
        session: Session,
        site: Site,
        thread: ConversationThread,
        turn: ConversationTurn,
        user_message: ConversationMessage,
        provider: ModelProvider,
        registry: ToolRegistry,
        mode: str,
        input_context: dict[str, Any],
        max_tool_iterations: int,
        build_context: ContextBuilder,
        write_event: EventWriter,
        serialize_proposal: ProposalSerializer,
    ):
        self.session = session
        self.site = site
        self.thread = thread
        self.turn = turn
        self.user_message = user_message
        self.provider = provider
        self.registry = registry
        self.mode = mode if mode in {"conversation", "setup", "commissioning", "operation", "debug"} else "setup"
        self.input_context = input_context
        self.max_tool_iterations = max_tool_iterations
        self.build_context = build_context
        self.write_event = write_event
        self.serialize_proposal = serialize_proposal
        self.events: list[AgentTurnEventRead] = []

    def run(self) -> RuntimeResult:
        observations: list[ModelObservation] = []
        available_tools = [spec.model_dump(mode="json") for spec in self.registry.specs_for_mode(self.mode)]
        self._record(
            "agent_runtime_started",
            {
                "provider": self.provider.provider_name,
                "mode": self.mode,
                "max_tool_iterations": self.max_tool_iterations,
                "available_tools": [tool["name"] for tool in available_tools],
            },
        )

        for iteration_index in range(self.max_tool_iterations):
            response = self._request_model_action(
                available_tools=available_tools,
                observations=observations,
                force_final=False,
                iteration_index=iteration_index,
            )
            action = response.action
            if isinstance(action, ModelFinalAnswer):
                self._record(
                    "model_final_answer",
                    {
                        "provider": response.provider_name,
                        "model": response.model,
                        "latency_ms": response.latency_ms,
                        "token_usage": response.token_usage,
                    },
                )
                return RuntimeResult(final_answer=action.content, events=self.events)
            if not isinstance(action, ModelToolCall):
                raise RuntimeError("Model returned an unsupported action.")

            validation = self._validate_tool_call(action)
            self._record(
                "model_action_validation",
                {
                    "iteration": iteration_index + 1,
                    "tool_name": action.name,
                    "valid": validation["valid"],
                    "reason": validation["reason"],
                },
            )
            if not validation["valid"]:
                observations.append(self._tool_validation_observation(action, validation["reason"]))
                continue

            observation = self._execute_tool_call(action)
            observations.append(observation)

        warning = (
            f"AgentRuntime reached max_tool_iterations={self.max_tool_iterations}; requesting a final answer "
            "from the model based on current observations."
        )
        self._record(
            "runtime_warning",
            {
                "warning": warning,
                "max_tool_iterations": self.max_tool_iterations,
                "observation_count": len(observations),
            },
        )
        response = self._request_model_action(
            available_tools=[],
            observations=observations,
            force_final=True,
            iteration_index=self.max_tool_iterations,
        )
        if not isinstance(response.action, ModelFinalAnswer):
            raise RuntimeError("AgentRuntime reached the tool iteration limit and the model did not return a final answer.")
        self._record(
            "model_final_answer",
            {
                "provider": response.provider_name,
                "model": response.model,
                "latency_ms": response.latency_ms,
                "token_usage": response.token_usage,
                "after_max_iterations": True,
            },
        )
        return RuntimeResult(final_answer=response.action.content, events=self.events, warnings=[warning])

    def _request_model_action(
        self,
        *,
        available_tools: list[dict[str, Any]],
        observations: list[ModelObservation],
        force_final: bool,
        iteration_index: int,
    ) -> ModelResponse:
        context = self.build_context(self.session, self.thread, self.input_context, available_tools)
        request = ModelRequest(
            turn_id=self.turn.id,
            user_message=self.user_message.content,
            recent_messages=context.get("recent_messages", []),
            context=context,
            available_tools=available_tools,
            observations=observations,
            force_final=force_final,
            max_tool_iterations=self.max_tool_iterations,
        )
        self._record(
            "model_request",
            {
                "iteration": iteration_index + 1,
                "provider": self.provider.provider_name,
                "force_final": force_final,
                "available_tools": [tool["name"] for tool in available_tools],
                "context_snapshot": context,
                "observations": [jsonable_encoder(observation) for observation in observations],
            },
        )
        try:
            response = self.provider.next_action(request)
        except ProviderError as exc:
            payload = {
                "provider": self.provider.provider_name,
                "message": str(exc),
                "iteration": iteration_index + 1,
            }
            if exc.raw_text:
                payload["raw_text"] = exc.raw_text
            if exc.raw_payload:
                payload["raw_payload"] = exc.raw_payload
            self._record("provider_error", payload)
            raise

        self._record(
            "provider_response",
            {
                "iteration": iteration_index + 1,
                "provider": response.provider_name,
                "model": response.model,
                "latency_ms": response.latency_ms,
                "token_usage": response.token_usage,
                "raw_text": response.raw_text,
                "raw_payload": response.raw_payload,
            },
        )
        action_payload: dict[str, Any]
        if isinstance(response.action, ModelToolCall):
            action_payload = {
                "type": "tool_call",
                "tool_call": {
                    "name": response.action.name,
                    "arguments": response.action.arguments,
                },
            }
        else:
            action_payload = {"type": "final_answer"}
        self._record(
            "model_action",
            {
                "iteration": iteration_index + 1,
                "provider": response.provider_name,
                "model": response.model,
                "latency_ms": response.latency_ms,
                "token_usage": response.token_usage,
                "action": action_payload,
                "raw_text": response.raw_text,
            },
        )
        return response

    def _validate_tool_call(self, action: ModelToolCall) -> dict[str, Any]:
        tool = self.registry.get(action.name)
        if tool is None:
            return {"valid": False, "reason": f"Unknown agent tool: {action.name}"}
        if self.mode not in tool.contexts:
            return {"valid": False, "reason": f"Tool {action.name} is not available in {self.mode} context."}
        if getattr(tool, "confirmation_policy", "") == "user_decision_required" and action.name != "role.prepare_binding_proposal":
            return {"valid": False, "reason": f"Tool {action.name} requires a user-decision workflow and is not model-applicable."}
        return {"valid": True, "reason": "validated"}

    def _tool_validation_observation(self, action: ModelToolCall, reason: str) -> ModelObservation:
        output = {
            "status": "rejected",
            "error_type": "tool_validation_error",
            "tool_name": action.name,
        }
        observation = ModelObservation(tool_name=action.name, output=output, error=reason)
        self._record(
            "model_observation",
            {
                "tool_name": action.name,
                "output": output,
                "error": reason,
            },
        )
        return observation

    def _execute_tool_call(self, action: ModelToolCall) -> ModelObservation:
        started = self._record(
            "tool_started",
            {"tool_name": action.name, "arguments": action.arguments},
        )
        tool_context = AgentToolContext(
            session=self.session,
            site=self.site,
            thread=self.thread,
            turn=self.turn,
            user_message=self.user_message.content,
            input_context=self.input_context,
            mode=self.mode,
        )
        try:
            invocation_id, result = execute_registered_tool(
                self.registry,
                tool_context,
                tool_name=action.name,
                arguments=action.arguments,
            )
        except Exception as exc:
            self.session.rollback()
            error = str(exc)
            output = {
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "tool_name": action.name,
            }
            finished = self._record(
                "tool_failed",
                {
                    "tool_name": action.name,
                    "result": output,
                    "error": error,
                },
            )
            self._record(
                "model_observation",
                {
                    "tool_name": action.name,
                    "output": output,
                    "error": error,
                    "event_refs": {
                        "tool_started_at": started.created_at,
                        "tool_failed_at": finished.created_at,
                    },
                },
            )
            return ModelObservation(tool_name=action.name, output=output, error=error)
        finished = self._record(
            "tool_finished",
            {
                "tool_name": action.name,
                "tool_invocation_id": invocation_id,
                "result": result.output,
            },
        )

        ui_events_payload = [event.model_dump(mode="json") for event in result.ui_events]
        if ui_events_payload:
            self._record("ui_events", {"events": ui_events_payload})

        for proposal_ref in result.created_proposal_refs:
            proposal = self.session.get(Proposal, proposal_ref)
            decision_request = self.session.scalar(
                select(UserDecisionRequest)
                .where(UserDecisionRequest.proposal_id == proposal_ref)
                .order_by(UserDecisionRequest.created_at.desc())
                .limit(1)
            )
            if proposal is None:
                continue
            proposal_payload = self.serialize_proposal(proposal, decision_request).model_dump(mode="json")
            self._record("proposal_created", proposal_payload)

        for decision_request_ref in result.created_decision_request_refs:
            decision_request = self.session.get(UserDecisionRequest, decision_request_ref)
            if decision_request is None:
                continue
            self._record(
                "decision_request_created",
                {
                    "decision_request_id": decision_request.id,
                    "proposal_id": decision_request.proposal_id,
                    "question": decision_request.question,
                    "options": decision_request.options or [],
                    "risk_level": decision_request.risk_level,
                    "status": decision_request.status,
                },
            )

        self._record(
            "model_observation",
            {
                "tool_name": action.name,
                "tool_invocation_id": invocation_id,
                "output": result.output,
                "ui_events": ui_events_payload,
                "event_refs": {
                    "tool_started_at": started.created_at,
                    "tool_finished_at": finished.created_at,
                },
                "created_proposal_refs": result.created_proposal_refs,
                "created_decision_request_refs": result.created_decision_request_refs,
            },
        )
        return ModelObservation(
            tool_name=action.name,
            invocation_id=invocation_id,
            output=result.output,
            ui_events=ui_events_payload,
        )

    def _record(self, event_type: str, payload: dict[str, Any]) -> AgentTurnEventRead:
        event = self.write_event(self.session, self.turn, event_type, payload)
        self.events.append(event)
        return event
