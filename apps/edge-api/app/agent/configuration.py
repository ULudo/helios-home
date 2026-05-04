from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    label: str
    description: str
    transport: str
    auth_kind: str
    base_url_default: str | None
    model_placeholder: str
    supports_base_url: bool = True
    supports_model: bool = True


@dataclass(slots=True)
class ProviderState:
    provider_id: str
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None


@dataclass(slots=True)
class AgentProviderConfig:
    selected_provider: str
    providers: dict[str, ProviderState]


@dataclass(frozen=True, slots=True)
class ProviderRuntimeStatus:
    selected_provider: str
    effective_provider: str
    ready: bool
    message: str
    state: ProviderState
    spec: ProviderSpec


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "stub": ProviderSpec(
        provider_id="stub",
        label="Diagnostics stub",
        description="Diagnostic-only placeholder; normal agent turns require a configured model provider.",
        transport="stub",
        auth_kind="none",
        base_url_default=None,
        model_placeholder="Not required",
        supports_base_url=False,
        supports_model=False,
    ),
    "openai": ProviderSpec(
        provider_id="openai",
        label="OpenAI",
        description="Hosted OpenAI API over the Responses interface.",
        transport="openai_responses",
        auth_kind="api_key",
        base_url_default="https://api.openai.com/v1",
        model_placeholder="Enter a model id",
    ),
    "anthropic": ProviderSpec(
        provider_id="anthropic",
        label="Anthropic",
        description="Hosted Anthropic Messages API.",
        transport="anthropic",
        auth_kind="api_key",
        base_url_default="https://api.anthropic.com",
        model_placeholder="Enter a model id",
    ),
    "openrouter": ProviderSpec(
        provider_id="openrouter",
        label="OpenRouter",
        description="OpenAI-compatible routed provider endpoint.",
        transport="openai_compatible",
        auth_kind="api_key",
        base_url_default="https://openrouter.ai/api/v1",
        model_placeholder="Enter a model id",
    ),
    "ollama": ProviderSpec(
        provider_id="ollama",
        label="Ollama",
        description="Local Ollama runtime on this machine or the local network.",
        transport="ollama",
        auth_kind="none",
        base_url_default="http://127.0.0.1:11434",
        model_placeholder="Enter a local model id",
    ),
    "custom_openai": ProviderSpec(
        provider_id="custom_openai",
        label="OpenAI-compatible",
        description="Custom OpenAI-compatible endpoint such as a local gateway or self-hosted proxy.",
        transport="openai_compatible",
        auth_kind="api_key",
        base_url_default="http://127.0.0.1:4000/v1",
        model_placeholder="Enter a model id",
    ),
}


def list_provider_specs() -> list[ProviderSpec]:
    return [PROVIDER_SPECS[key] for key in ("stub", "openai", "anthropic", "openrouter", "ollama", "custom_openai")]


def _default_provider_state(provider_id: str) -> ProviderState:
    spec = PROVIDER_SPECS[provider_id]
    return ProviderState(
        provider_id=provider_id,
        model="",
        base_url=spec.base_url_default,
        api_key=None,
    )


def _default_config() -> AgentProviderConfig:
    settings = get_settings()
    selected_provider = settings.agent_provider if settings.agent_provider in PROVIDER_SPECS else "stub"
    return AgentProviderConfig(
        selected_provider=selected_provider,
        providers={provider_id: _default_provider_state(provider_id) for provider_id in PROVIDER_SPECS},
    )


def _config_path() -> Path:
    return Path(get_settings().agent_config_path).expanduser()


def _serialize_config(config: AgentProviderConfig) -> dict[str, Any]:
    return {
        "selected_provider": config.selected_provider,
        "providers": {
            provider_id: {
                "model": state.model,
                "base_url": state.base_url,
                "api_key": state.api_key,
            }
            for provider_id, state in config.providers.items()
        },
    }


def _load_raw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_agent_provider_config() -> AgentProviderConfig:
    path = _config_path()
    raw = _load_raw_config(path)
    config = _default_config()
    selected_provider = str(raw.get("selected_provider") or config.selected_provider).strip()
    if selected_provider in PROVIDER_SPECS:
        config.selected_provider = selected_provider

    raw_providers = raw.get("providers")
    if isinstance(raw_providers, dict):
        for provider_id, spec in PROVIDER_SPECS.items():
            provider_raw = raw_providers.get(provider_id)
            if not isinstance(provider_raw, dict):
                continue
            config.providers[provider_id] = ProviderState(
                provider_id=provider_id,
                model=str(provider_raw.get("model") or "").strip(),
                base_url=str(provider_raw.get("base_url") or spec.base_url_default or "").strip() or None,
                api_key=str(provider_raw.get("api_key") or "").strip() or None,
            )
    return config


def save_agent_provider_config(config: AgentProviderConfig) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_serialize_config(config), indent=2, sort_keys=True)
    path.write_text(payload + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def resolve_provider_status(config: AgentProviderConfig | None = None) -> ProviderRuntimeStatus:
    config = config or load_agent_provider_config()
    selected_provider = config.selected_provider if config.selected_provider in PROVIDER_SPECS else "stub"
    state = config.providers.get(selected_provider) or _default_provider_state(selected_provider)
    spec = PROVIDER_SPECS[selected_provider]
    base_url = (state.base_url or spec.base_url_default or "").strip()
    model = state.model.strip()
    api_key = (state.api_key or "").strip()

    if selected_provider == "stub":
        return ProviderRuntimeStatus(
            selected_provider=selected_provider,
            effective_provider="stub",
            ready=False,
            message="The diagnostics stub is selected. Configure a model provider to operate Helios agent turns.",
            state=state,
            spec=spec,
        )

    missing: list[str] = []
    if spec.supports_model and not model:
        missing.append("model")
    if spec.supports_base_url and not base_url:
        missing.append("base URL")
    if spec.auth_kind == "api_key" and not api_key:
        missing.append("API key")

    if missing:
        joined = ", ".join(missing)
        return ProviderRuntimeStatus(
            selected_provider=selected_provider,
            effective_provider=selected_provider,
            ready=False,
            message=f"{spec.label} is selected but not ready yet. Missing: {joined}. Configure the provider before starting agent turns.",
            state=state,
            spec=spec,
        )

    return ProviderRuntimeStatus(
        selected_provider=selected_provider,
        effective_provider=selected_provider,
        ready=True,
        message=f"{spec.label} is configured and ready for agent responses.",
        state=state,
        spec=spec,
    )


def provider_is_ready(provider_id: str, config: AgentProviderConfig | None = None) -> bool:
    config = config or load_agent_provider_config()
    if provider_id not in PROVIDER_SPECS:
        return False
    original_selected = config.selected_provider
    config.selected_provider = provider_id
    try:
        status = resolve_provider_status(config)
    finally:
        config.selected_provider = original_selected
    return status.ready and status.effective_provider == provider_id


def upsert_agent_provider_config(
    *,
    provider_id: str,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    select_provider: bool = True,
) -> AgentProviderConfig:
    if provider_id not in PROVIDER_SPECS:
        raise KeyError(provider_id)
    config = load_agent_provider_config()
    current = config.providers.get(provider_id) or _default_provider_state(provider_id)
    spec = PROVIDER_SPECS[provider_id]
    next_state = ProviderState(
        provider_id=provider_id,
        model=current.model,
        base_url=current.base_url or spec.base_url_default,
        api_key=current.api_key,
    )
    if model is not None:
        next_state.model = model.strip()
    if base_url is not None:
        next_state.base_url = base_url.strip() or spec.base_url_default
    if clear_api_key:
        next_state.api_key = None
    elif api_key is not None and api_key.strip():
        next_state.api_key = api_key.strip()
    config.providers[provider_id] = next_state
    if select_provider:
        config.selected_provider = provider_id
    save_agent_provider_config(config)
    return config
