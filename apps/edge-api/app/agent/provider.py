from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.agent.configuration import PROVIDER_SPECS, ProviderRuntimeStatus


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _contains_any(text: str, tokens: tuple[str, ...] | list[str]) -> bool:
    normalized = _normalize(text)
    return any(token in normalized for token in tokens)


def _stringify_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts = [f"{key}: {_stringify_json(inner)}" for key, inner in value.items()]
        return "{ " + ", ".join(parts) + " }"
    if isinstance(value, list):
        return "[ " + ", ".join(_stringify_json(item) for item in value) + " ]"
    return str(value)


@dataclass(slots=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProposalRequest:
    action_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TurnDecision:
    tool_calls: list[ToolRequest] = field(default_factory=list)
    proposal_requests: list[ProposalRequest] = field(default_factory=list)
    immediate_response: str | None = None


class AgentProvider(Protocol):
    provider_name: str

    def decide_turn(self, context: dict[str, Any], user_message: str) -> TurnDecision:
        ...

    def compose_turn(
        self,
        context: dict[str, Any],
        user_message: str,
        tool_results: dict[str, Any],
        created_proposals: list[dict[str, Any]],
    ) -> str:
        ...


class StubAgentProvider:
    provider_name = "stub"

    def decide_turn(self, context: dict[str, Any], user_message: str) -> TurnDecision:
        normalized = _normalize(user_message)
        devices = context.get("devices", [])
        pending_proposals = context.get("pending_proposals", [])
        reachable_subnets = context.get("reachable_subnets", [])
        current_scope = set(context.get("current_subnets", []))
        referenced_subnets = [
            entry["cidr"]
            for entry in reachable_subnets
            if str(entry.get("cidr", "")) and str(entry.get("cidr", "")) in normalized
        ]

        if pending_proposals and normalized in {"yes", "yeah", "yep", "correct", "confirm", "do it", "sounds good"}:
            return TurnDecision(tool_calls=[ToolRequest(name="confirm_latest_proposal")])

        if pending_proposals and normalized in {"no", "nope", "reject", "cancel", "not that one"}:
            return TurnDecision(tool_calls=[ToolRequest(name="reject_latest_proposal")])

        if _contains_any(normalized, ("scan", "discover", "refresh", "look again", "erneut", "suche", "scan my")):
            return TurnDecision(tool_calls=[ToolRequest(name="refresh_discovery")])

        if _contains_any(normalized, ("all networks", "scan everything", "alle netze", "ganze netz", "all subnets")):
            all_subnets = [entry["cidr"] for entry in reachable_subnets]
            if all_subnets and set(all_subnets) != current_scope:
                return TurnDecision(
                    proposal_requests=[
                        ProposalRequest(
                            action_type="update_site_scope",
                            summary=f"Use all reachable subnets for discovery ({len(all_subnets)} network segment(s)).",
                            payload={"local_subnet": ", ".join(all_subnets)},
                        )
                    ]
                )

        if referenced_subnets:
            selected = ", ".join(referenced_subnets)
            if set(referenced_subnets) != current_scope:
                return TurnDecision(
                    proposal_requests=[
                        ProposalRequest(
                            action_type="update_site_scope",
                            summary=f"Use {selected} for discovery.",
                            payload={"local_subnet": selected},
                        )
                    ]
                )

        if _contains_any(normalized, ("what do you see", "what can you see", "was hast du", "what have you found")):
            return TurnDecision(immediate_response=self._device_summary(devices))

        requested_system = self._requested_system_type(normalized)
        if requested_system is not None:
            matches = self._matching_devices(devices, requested_system)
            if len(matches) == 1:
                device = matches[0]
                return TurnDecision(
                    proposal_requests=[
                        ProposalRequest(
                            action_type="confirm_system_binding",
                            summary=f"Confirm {device['name']} as the home's {requested_system.replace('_', ' ')}.",
                            payload={
                                "system_type": requested_system,
                                "label": device["name"],
                                "device_id": device["id"],
                                "device_name": device["name"],
                            },
                        )
                    ]
                )
            if len(matches) > 1:
                names = ", ".join(device["name"] for device in matches[:4])
                return TurnDecision(
                    immediate_response=f"I found multiple possible {requested_system.replace('_', ' ')} devices: {names}. Tell me which one looks right and I will bind it into the setup."
                )
            return TurnDecision(
                tool_calls=[
                    ToolRequest(
                        name="open_debug_case",
                        arguments={"device_type": requested_system, "notes": user_message},
                    ),
                    ToolRequest(name="run_latest_debug_probe"),
                ]
            )

        if not devices:
            return TurnDecision(
                immediate_response="I do not have any detected devices yet. Ask me to scan the house and I will start discovery."
            )

        return TurnDecision(
            immediate_response="I can scan the house, explain what I found, help you identify systems like the heat pump or battery, and propose setup changes for confirmation."
        )

    def compose_turn(
        self,
        context: dict[str, Any],
        user_message: str,
        tool_results: dict[str, Any],
        created_proposals: list[dict[str, Any]],
    ) -> str:
        if "confirm_latest_proposal" in tool_results:
            result = tool_results["confirm_latest_proposal"]
            return f"I applied that confirmation. {result.get('summary', 'The setup state is updated.')}"

        if "reject_latest_proposal" in tool_results:
            result = tool_results["reject_latest_proposal"]
            return f"I left the current setup unchanged. {result.get('summary', 'The proposal was rejected.')}"

        if "refresh_discovery" in tool_results:
            result = tool_results["refresh_discovery"]
            return (
                f"I refreshed discovery and now see {result.get('integrated_devices', 0)} integrated devices "
                f"from {', '.join(result.get('source_names', [])) or 'the configured local sources'}."
            )

        if "run_latest_debug_probe" in tool_results:
            report = tool_results.get("run_latest_debug_probe") or {}
            diagnosis = report.get("diagnosis", {})
            summary = diagnosis.get("summary") or "I checked the claim and refined the diagnosis."
            next_actions = diagnosis.get("next_actions", [])
            if next_actions:
                return f"{summary} Next, I recommend: {next_actions[0]}"
            return summary

        if "open_debug_case" in tool_results:
            report = tool_results["open_debug_case"]
            diagnosis = report.get("diagnosis", {})
            return diagnosis.get("summary") or "I opened a debug case for that device claim."

        if created_proposals:
            summary = created_proposals[0].get("summary", "I prepared a confirmation step.")
            if created_proposals[0].get("action_type") == "confirm_system_binding":
                return f"I found a likely match. Please confirm it: {summary}"
            return f"I prepared the next setup action for confirmation: {summary}"

        immediate = self.decide_turn(context, user_message).immediate_response
        return immediate or "I updated the setup context."

    def _requested_system_type(self, normalized: str) -> str | None:
        if _contains_any(normalized, ("heat pump", "wärmepumpe", "heizung")):
            return "heat_pump"
        if _contains_any(normalized, ("battery", "batterie", "storage")):
            return "battery"
        if _contains_any(normalized, ("pv", "solar", "inverter", "wechselrichter")):
            return "pv_inverter"
        if _contains_any(normalized, ("wallbox", "ev", "charger", "auto laden")):
            return "ev_charger"
        return None

    def _matching_devices(self, devices: list[dict[str, Any]], system_type: str) -> list[dict[str, Any]]:
        aliases = {
            "heat_pump": {"heat_pump"},
            "battery": {"battery"},
            "pv_inverter": {"pv_inverter"},
            "ev_charger": {"wallbox", "ev_charger"},
        }
        return [
            device
            for device in devices
            if device.get("device_type") in aliases.get(system_type, {system_type})
            or system_type.replace("_", " ") in _normalize(device.get("name", ""))
        ]

    def _device_summary(self, devices: list[dict[str, Any]]) -> str:
        if not devices:
            return "I do not see any devices yet. Ask me to run discovery and I will scan the configured local networks."
        by_type: dict[str, int] = {}
        for device in devices:
            device_type = str(device.get("device_type", "unknown"))
            by_type[device_type] = by_type.get(device_type, 0) + 1
        summary = ", ".join(f"{count} {device_type.replace('_', ' ')}" for device_type, count in sorted(by_type.items()))
        return f"I currently see {len(devices)} devices: {summary}."


class LLMBackedAgentProvider:
    def __init__(self, runtime: ProviderRuntimeStatus):
        self.provider_name = runtime.effective_provider
        self.runtime = runtime
        self._fallback = StubAgentProvider()

    def decide_turn(self, context: dict[str, Any], user_message: str) -> TurnDecision:
        return self._fallback.decide_turn(context, user_message)

    def compose_turn(
        self,
        context: dict[str, Any],
        user_message: str,
        tool_results: dict[str, Any],
        created_proposals: list[dict[str, Any]],
    ) -> str:
        fallback_text = self._fallback.compose_turn(context, user_message, tool_results, created_proposals)
        try:
            return self._complete_response(context, user_message, tool_results, created_proposals, fallback_text)
        except Exception as exc:
            return (
                f"{fallback_text}\n\n"
                f"I could not reach the configured {self.runtime.spec.label} provider, so I used the local fallback instead. "
                f"Details: {exc}"
            )

    def _complete_response(
        self,
        context: dict[str, Any],
        user_message: str,
        tool_results: dict[str, Any],
        created_proposals: list[dict[str, Any]],
        fallback_text: str,
    ) -> str:
        spec = self.runtime.spec
        state = self.runtime.state
        prompt_system = self._system_prompt()
        prompt_user = self._user_prompt(context, user_message, tool_results, created_proposals, fallback_text)

        if spec.transport == "openai_compatible":
            return self._complete_openai_compatible(prompt_system, prompt_user, state)
        if spec.transport == "openai_responses":
            return self._complete_openai_responses(prompt_system, prompt_user, state)
        if spec.transport == "anthropic":
            return self._complete_anthropic(prompt_system, prompt_user, state)
        if spec.transport == "ollama":
            return self._complete_ollama(prompt_system, prompt_user, state)
        raise RuntimeError(f"Unsupported provider transport: {spec.transport}")

    def _system_prompt(self) -> str:
        return (
            "You are Helios, a local-first home energy setup assistant. "
            "Respond clearly and concretely. Do not invent devices, capabilities, or completed actions. "
            "Use the provided context exactly. If a confirmation is pending, explicitly ask the user to confirm it. "
            "Prefer plain language over technical jargon. Keep the reply compact but useful."
        )

    def _user_prompt(
        self,
        context: dict[str, Any],
        user_message: str,
        tool_results: dict[str, Any],
        created_proposals: list[dict[str, Any]],
        fallback_text: str,
    ) -> str:
        devices = context.get("devices", [])
        pending = context.get("pending_proposals", [])
        reachable_subnets = context.get("reachable_subnets", [])
        setup_profile = context.get("setup_profile", {})
        device_lines = [
            f"- {device.get('name', 'Unknown')} ({device.get('device_type', 'unknown')}, {device.get('status', 'unknown')})"
            for device in devices[:12]
        ]
        proposal_lines = [
            f"- {proposal.get('summary', 'Pending action')} ({proposal.get('action_type', 'unknown')})"
            for proposal in created_proposals[:4]
        ]
        tool_lines = [
            f"- {name}: {_stringify_json(result)}"
            for name, result in tool_results.items()
        ]
        subnet_lines = [
            f"- {entry.get('cidr', 'unknown')} via {entry.get('interface', 'unknown')}"
            for entry in reachable_subnets[:8]
        ]
        prompt_lines = [
            f"User message:\n{user_message}",
            "",
            f"Current selected discovery scope: {', '.join(context.get('current_subnets', [])) or 'none'}",
            "Reachable subnets:",
            *(subnet_lines or ["- none discovered"]),
            "",
            "Detected devices:",
            *(device_lines or ["- none detected"]),
            "",
            f"Setup profile summary: {setup_profile.get('summary', 'No setup profile summary available.')}",
            f"Confirmed systems: {_stringify_json(setup_profile.get('confirmed_systems', []))}",
            f"Open setup questions: {_stringify_json(setup_profile.get('unresolved_items', []))}",
            "",
            "Tool results from this turn:",
            *(tool_lines or ["- no tool executed"]),
            "",
            "New pending confirmations created in this turn:",
            *(proposal_lines or ["- none"]),
            "",
            f"Existing pending confirmations: {len(pending)}",
            "",
            "Deterministic fallback draft:",
            fallback_text,
            "",
            "Write the final assistant reply in natural language. Do not mention JSON or internal prompt structure.",
        ]
        return "\n".join(prompt_lines)

    def _complete_openai_compatible(self, prompt_system: str, prompt_user: str, state) -> str:
        base_url = (state.base_url or PROVIDER_SPECS[self.provider_name].base_url_default or "").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if state.api_key:
            headers["Authorization"] = f"Bearer {state.api_key}"
        with httpx.Client(timeout=25.0) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": state.model,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": prompt_system},
                        {"role": "user", "content": prompt_user},
                    ],
                },
            )
            self._raise_for_status(response)
            payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("The provider returned no choices.")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [str(item.get("text", "")).strip() for item in content if isinstance(item, dict)]
            joined = "\n".join(part for part in parts if part)
            if joined:
                return joined
        raise RuntimeError("The provider returned an empty message.")

    def _complete_openai_responses(self, prompt_system: str, prompt_user: str, state) -> str:
        base_url = (state.base_url or PROVIDER_SPECS[self.provider_name].base_url_default or "").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if state.api_key:
            headers["Authorization"] = f"Bearer {state.api_key}"
        with httpx.Client(timeout=25.0) as client:
            response = client.post(
                f"{base_url}/responses",
                headers=headers,
                json={
                    "model": state.model,
                    "instructions": prompt_system,
                    "input": prompt_user,
                    "max_output_tokens": 900,
                },
            )
            self._raise_for_status(response)
            payload = response.json()

        output_text = str(payload.get("output_text", "")).strip()
        if output_text:
            return output_text

        output_items = payload.get("output") or []
        parts: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            for content_item in item.get("content") or []:
                if not isinstance(content_item, dict):
                    continue
                text = str(content_item.get("text", "")).strip()
                if text:
                    parts.append(text)
        joined = "\n".join(parts).strip()
        if joined:
            return joined
        raise RuntimeError("The provider returned an empty response.")

    def _complete_anthropic(self, prompt_system: str, prompt_user: str, state) -> str:
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
                    "max_tokens": 900,
                    "temperature": 0.2,
                    "system": prompt_system,
                    "messages": [
                        {"role": "user", "content": prompt_user},
                    ],
                },
            )
            self._raise_for_status(response)
            payload = response.json()
        content = payload.get("content") or []
        parts = [str(item.get("text", "")).strip() for item in content if isinstance(item, dict)]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined
        raise RuntimeError("The provider returned an empty message.")

    def _complete_ollama(self, prompt_system: str, prompt_user: str, state) -> str:
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
            return content
        raise RuntimeError("The provider returned an empty message.")

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            if detail:
                raise RuntimeError(f"{exc}. Response body: {detail}") from exc
            raise RuntimeError(str(exc)) from exc


def get_agent_provider(runtime: ProviderRuntimeStatus) -> AgentProvider:
    if runtime.effective_provider == "stub":
        return StubAgentProvider()
    return LLMBackedAgentProvider(runtime)
