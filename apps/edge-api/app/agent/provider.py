from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Literal, Protocol

import httpx

from app.agent.configuration import PROVIDER_SPECS, ProviderRuntimeStatus


def _stringify_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _supports_openai_reasoning_effort(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("gpt-5") or bool(re.match(r"^o\d", normalized))


def _openai_incomplete_reason(payload: dict[str, Any]) -> str:
    details = payload.get("incomplete_details")
    if isinstance(details, dict):
        return str(details.get("reason") or "")
    return ""


def _extract_openai_response_text(payload: dict[str, Any]) -> str:
    output_text = str(payload.get("output_text", "")).strip()
    if output_text:
        return output_text

    output_items = payload.get("output") or []
    parts: list[str] = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content_item in item.get("content") or []:
            if not isinstance(content_item, dict):
                continue
            text = str(content_item.get("text", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


@dataclass(slots=True)
class ModelToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelFinalAnswer:
    content: str


ModelAction = ModelToolCall | ModelFinalAnswer


@dataclass(slots=True)
class ModelObservation:
    tool_name: str
    output: dict[str, Any] = field(default_factory=dict)
    invocation_id: str = ""
    ui_events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


@dataclass(slots=True)
class ModelRequest:
    turn_id: str
    user_message: str
    recent_messages: list[dict[str, Any]]
    context: dict[str, Any]
    available_tools: list[dict[str, Any]]
    observations: list[ModelObservation] = field(default_factory=list)
    force_final: bool = False
    max_tool_iterations: int = 6


@dataclass(slots=True)
class ModelResponse:
    action: ModelAction
    provider_name: str
    model: str
    raw_text: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
    token_usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelError:
    message: str
    provider_name: str
    model: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, raw_text: str = "", raw_payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.raw_text = raw_text
        self.raw_payload = raw_payload or {}


class ModelProvider(Protocol):
    provider_name: str

    def next_action(self, request: ModelRequest) -> ModelResponse:
        ...


class DiagnosticStubModelProvider:
    provider_name = "stub"

    def next_action(self, request: ModelRequest) -> ModelResponse:
        raise ProviderError(
            "The development stub is diagnostic-only and cannot operate normal Helios agent turns. "
            "Configure a model provider to use Helios as a model-operated command center."
        )


class LLMModelProvider:
    def __init__(self, runtime: ProviderRuntimeStatus):
        self.provider_name = runtime.effective_provider
        self.runtime = runtime

    def next_action(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        response = self._complete_action(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        response.latency_ms = latency_ms
        return response

    def _system_prompt(self, request: ModelRequest, *, json_action: bool = False) -> str:
        final_constraint = (
            "No tools are available for this response. Answer naturally based only on the conversation, "
            "context, and observations."
            if request.force_final
            else "You may either call one typed tool through the provider tool interface or return a natural final answer."
        )
        lines = [
            "You are the configured Helios Home model operator.",
            "Helios-Home is the command center: it validates tools, executes deterministic workflows, persists state, and enforces safety.",
            "You own semantic interpretation, follow-up/reference resolution strategy, tool choice, evaluation of observations, planning, and the natural final answer.",
            "The Helios UI is a shared visual workspace between you and the user. Your response is not limited to text: use available UI tools as part of communicating what you mean when visual grounding, task/proposal presentation, or relationship display would make the answer clearer. Choose the tool that best supports your explanation; do not treat UI actions as decoration.",
            "For operational answers, be concrete and economical. State the current status, the reason or blocker, and the next executable step. Avoid dumping full inventories unless the user asks for detail.",
            "Never claim commissioning, telemetry validation, control, physical binding, or user approval unless it is explicitly present in tool observations or WorkStore/HomeGraph state.",
            "For EEBus, a user-reported SKI registration or ACK is evidence only; it is not proof of an active SHIP/SPINE connection. Use connection.establish or connection.inspect_readiness to verify current connection state, and treat SHIP as active only when a tool observation reports ship_runtime.status == ship_ready or an equivalent verified connection facet.",
            "The backend will reject unsafe or unavailable tool calls. The model must not ask tools to approve user decisions or apply physical control.",
            final_constraint,
        ]
        if json_action:
            lines.extend(
                [
                    "This provider does not expose a native tool-call channel here. Return strict JSON only, with one of these shapes:",
                    '{"type":"tool_call","tool_call":{"name":"tool.name","arguments":{}}}',
                    '{"type":"final_answer","final_answer":"natural assistant response"}',
                ]
            )
        return "\n".join(lines)

    def _user_prompt(self, request: ModelRequest, *, include_tool_schemas: bool) -> str:
        observations = [
            {
                "tool_name": observation.tool_name,
                "invocation_id": observation.invocation_id,
                "output": observation.output,
                "ui_events": observation.ui_events,
                "error": observation.error,
            }
            for observation in request.observations
        ]
        context = dict(request.context)
        if not include_tool_schemas:
            context["available_tools"] = [tool.get("name") for tool in request.available_tools]
        available_tools: list[dict[str, Any]] | list[str]
        if request.force_final:
            available_tools = []
        elif include_tool_schemas:
            available_tools = request.available_tools
        else:
            available_tools = [tool.get("name", "") for tool in request.available_tools]
        return "\n".join(
            [
                f"Turn id: {request.turn_id}",
                f"User message: {request.user_message}",
                f"Recent conversation: {_stringify_json(request.recent_messages)}",
                f"Available tools: {_stringify_json(available_tools)}",
                f"Context snapshot: {_stringify_json(context)}",
                f"Observations this turn: {_stringify_json(observations)}",
                f"Max tool iterations this turn: {request.max_tool_iterations}",
                (
                    "Choose the next action. Use the provider tool-call channel for state inspection or mutation; "
                    "return natural assistant text when you can answer from current context and observations."
                    if not include_tool_schemas
                    else "Choose the next action. Use tool_call JSON for state inspection or mutation; use final_answer JSON when you can answer naturally from current context and observations."
                ),
            ]
        )

    def _complete_action(self, request: ModelRequest) -> ModelResponse:
        spec = self.runtime.spec
        if spec.transport == "openai_compatible":
            return self._complete_openai_compatible_action(request)
        if spec.transport == "openai_responses":
            return self._complete_openai_responses_action(request)
        if spec.transport == "anthropic":
            return self._complete_json_fallback_action(request, self._complete_anthropic_text)
        if spec.transport == "ollama":
            return self._complete_json_fallback_action(request, self._complete_ollama_text)
        raise ProviderError(f"Unsupported provider transport: {spec.transport}")

    def _complete_openai_compatible_action(self, request: ModelRequest) -> ModelResponse:
        state = self.runtime.state
        base_url = (state.base_url or PROVIDER_SPECS[self.provider_name].base_url_default or "").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if state.api_key:
            headers["Authorization"] = f"Bearer {state.api_key}"
        request_payload: dict[str, Any] = {
            "model": state.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": self._system_prompt(request)},
                {"role": "user", "content": self._user_prompt(request, include_tool_schemas=False)},
            ],
        }
        name_map = _provider_tool_name_map(request.available_tools)
        if request.available_tools and not request.force_final:
            request_payload["tools"] = _openai_chat_tools(request.available_tools, name_map)
            request_payload["tool_choice"] = "auto"
            request_payload["parallel_tool_calls"] = False
        with httpx.Client(timeout=25.0) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=request_payload,
            )
            self._raise_for_status(response)
            payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("The provider returned no choices.", raw_payload=payload)
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if tool_calls and not request.force_final:
            action = _model_tool_call_from_openai_chat_tool_call(tool_calls[0], name_map)
            return ModelResponse(
                action=action,
                provider_name=self.provider_name,
                model=state.model,
                raw_payload=payload,
                token_usage=payload.get("usage") or {},
            )
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return ModelResponse(
                action=ModelFinalAnswer(content.strip()),
                provider_name=self.provider_name,
                model=state.model,
                raw_text=content.strip(),
                raw_payload=payload,
                token_usage=payload.get("usage") or {},
            )
        if isinstance(content, list):
            parts = [str(item.get("text", "")).strip() for item in content if isinstance(item, dict)]
            joined = "\n".join(part for part in parts if part)
            if joined:
                return ModelResponse(
                    action=ModelFinalAnswer(joined),
                    provider_name=self.provider_name,
                    model=state.model,
                    raw_text=joined,
                    raw_payload=payload,
                    token_usage=payload.get("usage") or {},
                )
        raise ProviderError("The provider returned an empty message.", raw_payload=payload)

    def _complete_openai_responses_action(self, request: ModelRequest) -> ModelResponse:
        state = self.runtime.state
        base_url = (state.base_url or PROVIDER_SPECS[self.provider_name].base_url_default or "").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if state.api_key:
            headers["Authorization"] = f"Bearer {state.api_key}"

        budgets = (2400, 6000)
        last_token_error = ""
        name_map = _provider_tool_name_map(request.available_tools)
        with httpx.Client(timeout=25.0) as client:
            for attempt_index, token_budget in enumerate(budgets):
                request_payload: dict[str, Any] = {
                    "model": state.model,
                    "instructions": self._system_prompt(request),
                    "input": self._user_prompt(request, include_tool_schemas=False),
                    "max_output_tokens": token_budget,
                }
                if request.available_tools and not request.force_final:
                    request_payload["tools"] = _openai_response_tools(request.available_tools, name_map)
                    request_payload["tool_choice"] = "auto"
                    request_payload["parallel_tool_calls"] = False
                if _supports_openai_reasoning_effort(state.model):
                    request_payload["reasoning"] = {"effort": "low"}

                response = client.post(
                    f"{base_url}/responses",
                    headers=headers,
                    json=request_payload,
                )
                self._raise_for_status(response)
                payload = response.json()

                action = _model_action_from_openai_responses_payload(payload, name_map, force_final=request.force_final)
                if action is not None:
                    output_text = action.content if isinstance(action, ModelFinalAnswer) else ""
                    return ModelResponse(
                        action=action,
                        provider_name=self.provider_name,
                        model=state.model,
                        raw_text=output_text,
                        raw_payload=payload,
                        token_usage=payload.get("usage") or {},
                    )

                incomplete_reason = _openai_incomplete_reason(payload)
                if payload.get("status") == "incomplete" and incomplete_reason in {"max_output_tokens", "max_tokens"}:
                    last_token_error = (
                        f"The OpenAI Responses API exhausted max_output_tokens={token_budget} before producing visible text."
                    )
                    if attempt_index < len(budgets) - 1:
                        continue
                    raise ProviderError(
                        f"{last_token_error} Retried once with a larger output budget and still received no text.",
                        raw_payload=payload,
                    )

                status = str(payload.get("status") or "unknown")
                error = payload.get("error")
                if error:
                    raise ProviderError(f"The OpenAI Responses API returned status {status}: {_stringify_json(error)}", raw_payload=payload)
                raise ProviderError(f"The OpenAI Responses API returned status {status} without a tool call or final answer.", raw_payload=payload)

        if last_token_error:
            raise ProviderError(last_token_error)
        raise ProviderError("The OpenAI Responses API returned no visible assistant text.")

    def _complete_json_fallback_action(self, request: ModelRequest, completer) -> ModelResponse:
        prompt_system = self._system_prompt(request, json_action=True)
        prompt_user = self._user_prompt(request, include_tool_schemas=True)
        text, usage, raw_payload = completer(prompt_system, prompt_user)
        try:
            action = _parse_model_action(text, force_final=request.force_final)
        except ProviderError as exc:
            if _looks_like_plain_final_answer(text):
                action = ModelFinalAnswer(text.strip())
            else:
                repair_text, repair_usage, repair_payload = completer(
                    "Convert the previous model response into the required ModelAction JSON. Return JSON only.",
                    "\n".join(
                        [
                            f"Previous response: {text}",
                            f"Parse error: {exc}",
                            "Required JSON shapes:",
                            '{"type":"tool_call","tool_call":{"name":"tool.name","arguments":{}}}',
                            '{"type":"final_answer","final_answer":"natural assistant response"}',
                        ]
                    ),
                )
                usage = _merge_usage(usage, repair_usage)
                raw_payload = {"initial": raw_payload, "repair": repair_payload}
                try:
                    action = _parse_model_action(repair_text, force_final=request.force_final)
                    text = repair_text
                except ProviderError as repair_exc:
                    raise ProviderError(
                        f"{repair_exc}. Retried once with a JSON repair prompt.",
                        raw_text=text,
                        raw_payload=raw_payload if isinstance(raw_payload, dict) else {},
                    ) from None
        return ModelResponse(
            action=action,
            provider_name=self.provider_name,
            model=self.runtime.state.model,
            raw_text=text,
            raw_payload=raw_payload if isinstance(raw_payload, dict) else {},
            token_usage=usage,
        )

    def _complete_anthropic_text(self, prompt_system: str, prompt_user: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        state = self.runtime.state
        base_url = (state.base_url or PROVIDER_SPECS[self.provider_name].base_url_default or "").rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": state.api_key or "",
            "anthropic-version": "2023-06-01",
        }
        with httpx.Client(timeout=25.0) as client:
            response = client.post(
                f"{base_url}/v1/messages",
                headers=headers,
                json={
                    "model": state.model,
                    "max_tokens": 2400,
                    "temperature": 0.2,
                    "system": prompt_system,
                    "messages": [{"role": "user", "content": prompt_user}],
                },
            )
            self._raise_for_status(response)
            payload = response.json()
        content = payload.get("content") or []
        parts = [str(item.get("text", "")).strip() for item in content if isinstance(item, dict)]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined, payload.get("usage") or {}, payload
        raise ProviderError("The provider returned an empty message.", raw_payload=payload)

    def _complete_ollama_text(self, prompt_system: str, prompt_user: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        state = self.runtime.state
        base_url = (state.base_url or PROVIDER_SPECS[self.provider_name].base_url_default or "").rstrip("/")
        with httpx.Client(timeout=45.0) as client:
            response = client.post(
                f"{base_url}/api/chat",
                json={
                    "model": state.model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": prompt_system},
                        {"role": "user", "content": prompt_user},
                    ],
                },
            )
            self._raise_for_status(response)
            payload = response.json()
        message = payload.get("message") or {}
        content = str(message.get("content", "")).strip()
        if content:
            return content, payload.get("usage") or {}, payload
        raise ProviderError("The provider returned an empty message.", raw_payload=payload)

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            if detail:
                raise ProviderError(f"{exc}. Response body: {detail}") from exc
            raise ProviderError(str(exc)) from exc


def _provider_tool_name(tool_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "__", tool_name)[:64]


def _provider_tool_name_map(available_tools: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for tool in available_tools:
        canonical = str(tool.get("name") or "").strip()
        if not canonical:
            continue
        external = _provider_tool_name(canonical)
        if external in used:
            suffix = 2
            base = external[:58]
            while f"{base}_{suffix}" in used:
                suffix += 1
            external = f"{base}_{suffix}"
        used.add(external)
        mapping[external] = canonical
    return mapping


def _provider_name_for_tool(canonical_name: str, name_map: dict[str, str]) -> str:
    for external, canonical in name_map.items():
        if canonical == canonical_name:
            return external
    return _provider_tool_name(canonical_name)


def _tool_parameters_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("input_schema")
    if isinstance(schema, dict) and schema:
        return schema
    return {"type": "object", "properties": {}}


def _openai_response_tools(available_tools: list[dict[str, Any]], name_map: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": _provider_name_for_tool(str(tool.get("name") or ""), name_map),
            "description": f"{tool.get('purpose', '')} Canonical Helios tool name: {tool.get('name', '')}".strip(),
            "parameters": _tool_parameters_schema(tool),
        }
        for tool in available_tools
    ]


def _openai_chat_tools(available_tools: list[dict[str, Any]], name_map: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _provider_name_for_tool(str(tool.get("name") or ""), name_map),
                "description": f"{tool.get('purpose', '')} Canonical Helios tool name: {tool.get('name', '')}".strip(),
                "parameters": _tool_parameters_schema(tool),
            },
        }
        for tool in available_tools
    ]


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    if raw_arguments is None or raw_arguments == "":
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Model tool_call arguments were not valid JSON: {exc}", raw_text=raw_arguments) from None
        if isinstance(parsed, dict):
            return parsed
    raise ProviderError("Model tool_call arguments must be an object.")


def _model_tool_call_from_openai_chat_tool_call(tool_call: dict[str, Any], name_map: dict[str, str]) -> ModelToolCall:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    external_name = str(function.get("name") or tool_call.get("name") or "").strip()
    canonical_name = name_map.get(external_name, external_name)
    return ModelToolCall(name=canonical_name, arguments=_parse_tool_arguments(function.get("arguments") or tool_call.get("arguments")))


def _model_action_from_openai_responses_payload(
    payload: dict[str, Any],
    name_map: dict[str, str],
    *,
    force_final: bool,
) -> ModelAction | None:
    output_items = payload.get("output") or []
    if not isinstance(output_items, list):
        return None
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call" and not force_final:
            external_name = str(item.get("name") or "").strip()
            canonical_name = name_map.get(external_name, external_name)
            return ModelToolCall(name=canonical_name, arguments=_parse_tool_arguments(item.get("arguments")))

    output_text = _extract_openai_response_text(payload)
    if output_text:
        return ModelFinalAnswer(output_text)
    return None


def _looks_like_plain_final_answer(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("{") and not stripped.startswith("[") and "tool_call" not in stripped[:200]


def _merge_usage(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    if not first:
        return second
    if not second:
        return first
    merged = dict(first)
    for key, value in second.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        else:
            merged[f"repair_{key}"] = value
    return merged


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ProviderError("Model response was not valid JSON.") from None
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ProviderError("Model action response must be a JSON object.")
    return payload


def _parse_model_action(text: str, *, force_final: bool = False) -> ModelAction:
    payload = _parse_json_object(text)
    action_type = str(payload.get("type") or "").strip()
    if action_type == "final_answer":
        content = str(payload.get("final_answer") or "").strip()
        if not content:
            raise ProviderError("Model returned an empty final_answer.")
        return ModelFinalAnswer(content=content)
    if action_type == "tool_call":
        if force_final:
            raise ProviderError("Model returned a tool_call when a final_answer was required.")
        tool_call = payload.get("tool_call")
        if not isinstance(tool_call, dict):
            raise ProviderError("Model tool_call action is missing the tool_call object.")
        name = str(tool_call.get("name") or "").strip()
        arguments = tool_call.get("arguments")
        if not name:
            raise ProviderError("Model tool_call action is missing a tool name.")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProviderError("Model tool_call arguments must be an object.")
        return ModelToolCall(name=name, arguments=arguments)
    raise ProviderError("Model action must be either tool_call or final_answer.")


def get_model_provider(runtime: ProviderRuntimeStatus) -> ModelProvider:
    if not runtime.ready:
        raise ProviderError(runtime.message)
    if runtime.effective_provider == "stub":
        return DiagnosticStubModelProvider()
    return LLMModelProvider(runtime)
