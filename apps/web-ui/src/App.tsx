import { useEffect, useMemo, useReducer, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { AppIcon, type AppIconName } from "./components/AppIcon";
import { CapabilityPill } from "./components/CapabilityPill";
import { StatusBadge } from "./components/StatusBadge";
import { api } from "./lib/api";
import type {
  ActionProposalRead,
  AgentMessageRead,
  AgentProviderConfigRead,
  AgentProviderOptionRead,
  AgentThreadRead,
  DeviceRead,
  OverviewResponse,
  ReachableSubnetRead,
} from "./lib/types";
import { createInitialUIState, parseUiActions, uiStateReducer } from "./lib/uiState";

type NavView = "overview" | "settings";

type NavItem = {
  view: NavView;
  label: string;
  icon: AppIconName;
};

type CanvasPoint = {
  x: number;
  y: number;
};

const NAV_ITEMS: NavItem[] = [
  { view: "overview", label: "Overview", icon: "overview" },
  { view: "settings", label: "Settings", icon: "settings" },
];

function humanize(value: string): string {
  return value.split("_").join(" ");
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function parseConfiguredSubnets(rawValue: string): string[] {
  return rawValue
    .split(/[\n,;]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function formatNumber(value: number): string {
  if (Math.abs(value) >= 100) {
    return value.toFixed(0);
  }
  if (Math.abs(value) >= 10) {
    return value.toFixed(1);
  }
  return value.toFixed(2).replace(/\.?0+$/, "");
}

function formatTelemetryValue(key: string, value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value !== "number") {
    return String(value);
  }
  if (key.endsWith("_w")) {
    const absolute = Math.abs(value);
    if (absolute >= 1000) {
      return `${formatNumber(value / 1000)} kW`;
    }
    return `${formatNumber(value)} W`;
  }
  if (key.endsWith("_kw")) {
    return `${formatNumber(value)} kW`;
  }
  if (key.endsWith("_pct") || key.includes("soc")) {
    return `${formatNumber(value)}%`;
  }
  if (key.endsWith("_c")) {
    return `${formatNumber(value)}°C`;
  }
  return formatNumber(value);
}

function formatTelemetryPair(key: string, value: string | number | boolean | null | undefined): string {
  return `${humanize(key)} ${formatTelemetryValue(key, value)}`;
}

function deviceTelemetrySummary(device: DeviceRead): string {
  const entries = Object.entries(device.telemetry)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .slice(0, 2);
  if (entries.length === 0) {
    return "No live telemetry";
  }
  return entries.map(([key, value]) => formatTelemetryPair(key, value)).join(" · ");
}

function deviceSpecTooltip(device: DeviceRead): string {
  const telemetrySummary = Object.entries(device.telemetry)
    .slice(0, 4)
    .map(([key, value]) => formatTelemetryPair(key, value))
    .join("\n");
  const protocols = device.protocols.length > 0 ? device.protocols.join(", ") : "None";

  return [
    device.name,
    `${device.manufacturer} · ${device.model}`,
    `Type: ${humanize(device.device_type)}`,
    `Protocols: ${protocols}`,
    `Firmware: ${device.firmware || "Unknown"}`,
    telemetrySummary ? `Telemetry:\n${telemetrySummary}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function deviceMatchesFocusedSystem(device: DeviceRead, systemType: string | null): boolean {
  if (!systemType) {
    return true;
  }

  const aliases: Record<string, string[]> = {
    heat_pump: ["heat_pump"],
    battery: ["battery"],
    pv_inverter: ["pv_inverter"],
    ev_charger: ["wallbox", "ev_charger"],
    grid_meter: ["grid_meter"],
    smart_appliance: ["smart_appliance", "other"],
  };

  const candidates = aliases[systemType] ?? [systemType];
  return candidates.includes(device.device_type);
}

function deviceIcon(deviceType: string): AppIconName {
  if (deviceType === "heat_pump") {
    return "hvac";
  }
  if (deviceType === "battery") {
    return "battery";
  }
  if (deviceType === "pv_inverter") {
    return "pv";
  }
  if (deviceType === "grid_meter") {
    return "grid";
  }
  if (deviceType === "wallbox" || deviceType === "ev_charger") {
    return "ev";
  }
  if (deviceType === "smart_appliance") {
    return "loads";
  }
  return "devices";
}

function messageCardClasses(role: string): string {
  if (role === "user") {
    return "ml-10 border-[#ecd29d] bg-[#fff6e5]";
  }
  if (role === "assistant") {
    return "mr-8 border-[#d7deea] bg-white";
  }
  return "mr-10 border-[#d7deea] bg-[#f6f8fb]";
}

function renderProposalSummary(proposal: ActionProposalRead): string {
  if (proposal.action_type === "confirm_system_binding") {
    return String(proposal.payload.label ?? proposal.summary);
  }
  if (proposal.action_type === "update_site_scope") {
    return String(proposal.payload.local_subnet ?? proposal.summary);
  }
  return proposal.summary;
}

function buildCanvasPoints(count: number): CanvasPoint[] {
  if (count <= 0) {
    return [];
  }

  const baseRadiusX = count <= 4 ? 260 : count <= 8 ? 300 : 340;
  const baseRadiusY = count <= 4 ? 190 : count <= 8 ? 230 : 270;

  return Array.from({ length: count }, (_, index) => {
    const angle = (-Math.PI / 2) + (index * (Math.PI * 2)) / count;
    const ringOffset = count > 8 && index % 2 === 0 ? 26 : 0;
    return {
      x: Math.cos(angle) * (baseRadiusX + ringOffset),
      y: Math.sin(angle) * (baseRadiusY + ringOffset),
    };
  });
}

function severityTone(severity: "info" | "caution" | "critical"): string {
  if (severity === "critical") {
    return "border-rose-200 bg-rose-50 text-rose-800";
  }
  if (severity === "caution") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  return "border-slate-200 bg-slate-50 text-slate-800";
}

function ProviderFormSection({
  agentProviderConfig,
  providerForm,
  selectedProviderOption,
  providerReadyForChat,
  busyAction,
  onProviderSelection,
  onProviderFormChange,
  onSave,
  onClearKey,
}: {
  agentProviderConfig: AgentProviderConfigRead | null;
  providerForm: {
    providerId: string;
    model: string;
    baseUrl: string;
    apiKey: string;
  };
  selectedProviderOption: AgentProviderOptionRead | null;
  providerReadyForChat: boolean;
  busyAction: string | null;
  onProviderSelection: (providerId: string) => void;
  onProviderFormChange: (next: Partial<{ providerId: string; model: string; baseUrl: string; apiKey: string }>) => void;
  onSave: () => void;
  onClearKey: () => void;
}) {
  return (
    <section className="surface-subtle p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="section-heading">Model provider</p>
          <p className="mt-2 text-sm leading-6 text-slate-600">
            {agentProviderConfig?.message ??
              "Choose a provider and model. Credentials stay on this machine and are never returned by the API after saving."}
          </p>
        </div>
        <span
          className={`rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.08em] ${
            providerReadyForChat ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-white text-slate-600"
          }`}
        >
          {providerReadyForChat ? "Ready" : "Setup"}
        </span>
      </div>

      <div className="mt-5 grid gap-4">
        <label className="grid gap-2 text-sm text-slate-700">
          <span className="font-medium">Provider</span>
          <select
            className="field-shell"
            value={providerForm.providerId}
            onChange={(event) => onProviderSelection(event.target.value)}
            disabled={busyAction !== null || agentProviderConfig === null}
          >
            {(agentProviderConfig?.provider_options ?? []).map((option) => (
              <option key={option.provider_id} value={option.provider_id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        {selectedProviderOption?.supports_model ? (
          <label className="grid gap-2 text-sm text-slate-700">
            <span className="font-medium">Model</span>
            <input
              className="field-shell"
              type="text"
              value={providerForm.model}
              placeholder={selectedProviderOption.model_placeholder}
              onChange={(event) => onProviderFormChange({ model: event.target.value })}
              disabled={busyAction !== null}
            />
          </label>
        ) : null}

        {selectedProviderOption?.supports_base_url ? (
          <label className="grid gap-2 text-sm text-slate-700">
            <span className="font-medium">Base URL</span>
            <input
              className="field-shell"
              type="text"
              value={providerForm.baseUrl}
              placeholder={selectedProviderOption.base_url_default ?? ""}
              onChange={(event) => onProviderFormChange({ baseUrl: event.target.value })}
              disabled={busyAction !== null}
            />
          </label>
        ) : null}

        {selectedProviderOption?.auth_kind === "api_key" ? (
          <label className="grid gap-2 text-sm text-slate-700">
            <span className="font-medium">API key</span>
            <input
              className="field-shell"
              type="password"
              value={providerForm.apiKey}
              placeholder={
                selectedProviderOption.api_key_configured ? "Stored locally. Leave blank to keep it." : "Paste the key once"
              }
              onChange={(event) => onProviderFormChange({ apiKey: event.target.value })}
              disabled={busyAction !== null}
            />
          </label>
        ) : null}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-2">
        <button type="button" className="primary-button" onClick={onSave} disabled={busyAction !== null}>
          Save provider
        </button>
        {selectedProviderOption?.auth_kind === "api_key" && selectedProviderOption.api_key_configured ? (
          <button type="button" className="secondary-button" onClick={onClearKey} disabled={busyAction !== null}>
            Clear key
          </button>
        ) : null}
      </div>
    </section>
  );
}

export default function App() {
  const [thread, setThread] = useState<AgentThreadRead | null>(null);
  const [agentProviderConfig, setAgentProviderConfig] = useState<AgentProviderConfigRead | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [reachableSubnets, setReachableSubnets] = useState<ReachableSubnetRead[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [navExpanded, setNavExpanded] = useState(false);
  const [inspectedDeviceId, setInspectedDeviceId] = useState<string | null>(null);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [streamingAssistant, setStreamingAssistant] = useState<AgentMessageRead | null>(null);
  const [providerForm, setProviderForm] = useState({
    providerId: "stub",
    model: "",
    baseUrl: "",
    apiKey: "",
  });
  const [uiState, dispatchUiState] = useReducer(uiStateReducer, undefined, createInitialUIState);

  const streamRef = useRef<EventSource | null>(null);
  const timelineRef = useRef<HTMLDivElement | null>(null);

  const allDevices = overview?.devices ?? [];
  const inspectedDevice = useMemo(
    () => allDevices.find((device) => device.id === inspectedDeviceId) ?? null,
    [allDevices, inspectedDeviceId],
  );

  const timelineMessages = useMemo(() => {
    const persisted = thread?.messages ?? [];
    if (!streamingAssistant) {
      return persisted;
    }
    return [...persisted, streamingAssistant];
  }, [streamingAssistant, thread?.messages]);

  const selectedProviderOption = useMemo<AgentProviderOptionRead | null>(() => {
    if (!agentProviderConfig) {
      return null;
    }
    return (
      agentProviderConfig.provider_options.find((option) => option.provider_id === providerForm.providerId) ??
      agentProviderConfig.provider_options.find((option) => option.selected) ??
      null
    );
  }, [agentProviderConfig, providerForm.providerId]);

  const providerReadyForChat = useMemo(() => {
    if (!agentProviderConfig) {
      return false;
    }
    return agentProviderConfig.ready && agentProviderConfig.effective_provider !== "stub";
  }, [agentProviderConfig]);

  const currentScope = useMemo(() => parseConfiguredSubnets(overview?.site.local_subnet ?? ""), [overview?.site.local_subnet]);
  const canvasPoints = useMemo(() => buildCanvasPoints(allDevices.length), [allDevices.length]);
  const currentView: NavView = uiState.currentView === "settings" ? "settings" : "overview";
  const unresolvedItems = thread?.setup_profile.unresolved_items ?? [];
  const confirmedSystems = thread?.setup_profile.confirmed_systems ?? [];

  function syncProviderForm(config: AgentProviderConfigRead, providerId?: string) {
    const option =
      config.provider_options.find((entry) => entry.provider_id === (providerId ?? config.selected_provider)) ??
      config.provider_options.find((entry) => entry.selected) ??
      config.provider_options[0];

    if (!option) {
      return;
    }

    setProviderForm({
      providerId: option.provider_id,
      model: option.model ?? "",
      baseUrl: option.base_url ?? option.base_url_default ?? "",
      apiKey: "",
    });
  }

  async function refreshAll() {
    const [nextThread, nextProviderConfig, nextOverview, nextSubnets] = await Promise.all([
      api.getAgentThread(),
      api.getAgentProviderConfig(),
      api.getOverview(),
      api.listReachableSubnets(),
    ]);

    setThread(nextThread);
    setAgentProviderConfig(nextProviderConfig);
    setOverview(nextOverview);
    setReachableSubnets(nextSubnets);
    syncProviderForm(nextProviderConfig);
  }

  useEffect(() => {
    async function bootstrap() {
      setLoading(true);
      setError(null);
      try {
        await refreshAll();
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : "Unable to load Helios Home.");
      } finally {
        setLoading(false);
      }
    }

    void bootstrap();

    return () => {
      streamRef.current?.close();
    };
  }, []);

  useEffect(() => {
    if (!timelineRef.current) {
      return;
    }
    timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
  }, [thread?.pending_proposals, timelineMessages]);

  useEffect(() => {
    if (inspectedDeviceId && !allDevices.some((device) => device.id === inspectedDeviceId)) {
      setInspectedDeviceId(null);
    }
  }, [allDevices, inspectedDeviceId]);

  function startStream(turnId: string) {
    streamRef.current?.close();
    streamRef.current = api.streamAgentTurn(turnId, {
      onEvent: (event) => {
        if (event.event_type === "ui_actions") {
          const actions = parseUiActions(event.payload.actions);
          if (actions.length > 0) {
            dispatchUiState({ type: "apply_actions", actions, occurredAt: event.created_at });
          }
        }

        if (event.event_type === "assistant_delta") {
          const delta = String(event.payload.delta ?? "");
          setStreamingAssistant((current) => {
            if (!current) {
              return {
                id: `stream-${turnId}`,
                role: "assistant",
                content: delta,
                status: "streaming",
                created_at: event.created_at,
                turn_id: turnId,
              };
            }
            return { ...current, content: `${current.content}${delta}` };
          });
        }

        if (event.event_type === "assistant_message_completed") {
          const nextMessage = event.payload.message as AgentMessageRead | undefined;
          const actions = parseUiActions(event.payload.ui_actions);
          if (actions.length > 0) {
            dispatchUiState({ type: "apply_actions", actions, occurredAt: event.created_at });
          }
          if (nextMessage) {
            setThread((current) => {
              if (!current) {
                return current;
              }
              return {
                ...current,
                messages: [...current.messages, nextMessage],
              };
            });
          }
          setStreamingAssistant(null);
        }

        if (event.event_type === "proposal_created") {
          setNotice("Helios prepared a setup confirmation.");
        }

        if (event.event_type === "error") {
          setError(String(event.payload.message ?? "The agent turn failed."));
        }
      },
      onError: (streamError) => {
        setActiveTurnId(null);
        setStreamingAssistant(null);
        setError(streamError.message);
      },
      onEnd: () => {
        setActiveTurnId(null);
        setStreamingAssistant(null);
        void refreshAll();
      },
    });
  }

  async function sendMessage(content: string) {
    const normalized = content.trim();
    if (!normalized || activeTurnId !== null) {
      return;
    }
    if (!providerReadyForChat) {
      dispatchUiState({ type: "set_view", view: "settings" });
      setNotice("Configure a model provider first. Then the right panel switches into chat.");
      return;
    }

    setBusyAction("send-message");
    setError(null);
    setNotice(null);

    try {
      const accepted = await api.createAgentMessage({ content: normalized });
      setThread((current) => {
        if (!current) {
          return current;
        }
        return {
          ...current,
          messages: [...current.messages, accepted.user_message],
        };
      });
      setDraft("");
      setStreamingAssistant({
        id: `stream-${accepted.turn_id}`,
        role: "assistant",
        content: "",
        status: "streaming",
        created_at: new Date().toISOString(),
        turn_id: accepted.turn_id,
      });
      setActiveTurnId(accepted.turn_id);
      startStream(accepted.turn_id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to send your message.");
      setStreamingAssistant(null);
      setActiveTurnId(null);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendMessage(draft);
  }

  async function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    await sendMessage(draft);
  }

  async function handleProposalDecision(proposalId: string, decision: "confirm" | "reject") {
    setBusyAction(`${decision}-proposal`);
    setError(null);
    setNotice(null);

    try {
      const result =
        decision === "confirm"
          ? await api.confirmAgentProposal(proposalId)
          : await api.rejectAgentProposal(proposalId);
      setThread(result.thread);
      setNotice(decision === "confirm" ? "Helios applied your confirmation." : "Helios left the current setup unchanged.");
      await refreshAll();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to apply that decision.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleProviderSave() {
    setBusyAction("save-provider");
    setError(null);
    setNotice(null);

    try {
      const nextConfig = await api.updateAgentProviderConfig({
        provider_id: providerForm.providerId,
        model: selectedProviderOption?.supports_model ? providerForm.model : null,
        base_url: selectedProviderOption?.supports_base_url ? providerForm.baseUrl : null,
        api_key: providerForm.apiKey.trim() || null,
      });
      setAgentProviderConfig(nextConfig);
      syncProviderForm(nextConfig, providerForm.providerId);
      setNotice("Helios updated the local model provider configuration.");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to update the provider configuration.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleProviderKeyClear() {
    setBusyAction("clear-provider-key");
    setError(null);
    setNotice(null);

    try {
      const nextConfig = await api.updateAgentProviderConfig({
        provider_id: providerForm.providerId,
        clear_api_key: true,
      });
      setAgentProviderConfig(nextConfig);
      syncProviderForm(nextConfig, providerForm.providerId);
      setNotice("Helios removed the stored provider key from this machine.");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to clear the provider key.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleRunDiscovery() {
    setBusyAction("run-discovery");
    setError(null);
    setNotice(null);

    try {
      const result = await api.runDiscovery();
      await refreshAll();
      setNotice(`Discovery finished. ${result.integrated_devices} device(s) are now in the workspace.`);
      dispatchUiState({ type: "set_view", view: "overview" });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to run discovery.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleUseReachableNetworks() {
    if (reachableSubnets.length === 0) {
      setNotice("No reachable networks are available on this machine.");
      return;
    }

    setBusyAction("apply-scope");
    setError(null);
    setNotice(null);

    try {
      const localSubnet = reachableSubnets.map((entry) => entry.cidr).join(", ");
      await api.updateSite({ local_subnet: localSubnet });
      await refreshAll();
      setNotice("Helios updated the site scope to the currently reachable networks.");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to update the site scope.");
    } finally {
      setBusyAction(null);
    }
  }

  function renderDeviceOverlay(device: DeviceRead) {
    return (
      <div className="absolute inset-0 z-30 bg-[rgba(247,249,252,0.78)] backdrop-blur-[6px]">
        <div className="h-full w-full p-6">
          <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-[32px] border border-[#d8dfea] bg-white shadow-[0_28px_72px_rgba(15,23,42,0.14)]">
            <header className="flex items-start justify-between gap-6 border-b border-[#d8dfea] px-8 py-7">
              <div className="flex min-w-0 items-start gap-4">
                <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-[18px] border border-[#f1d7a2] bg-[#fff7e8] text-[#c47d0f]">
                  <AppIcon name={deviceIcon(device.device_type)} className="h-6 w-6" />
                </div>
                <div className="min-w-0">
                  <p className="section-heading">Detected device</p>
                  <p className="m-0 mt-2 truncate text-2xl font-semibold tracking-[-0.02em] text-slate-950" title={device.name}>
                    {device.name}
                  </p>
                  <p className="m-0 mt-2 text-sm text-slate-500">
                    {device.manufacturer} · {device.model}
                  </p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <StatusBadge status={device.primary_status} />
                <button
                  type="button"
                  className="secondary-button h-10 w-10 px-0"
                  onClick={() => setInspectedDeviceId(null)}
                  aria-label="Close device details"
                >
                  ×
                </button>
              </div>
            </header>

            <div className="subtle-scrollbar min-h-0 flex-1 overflow-y-auto px-8 py-7">
              <div className="grid gap-6 xl:grid-cols-[minmax(0,1.2fr)_360px]">
                <div className="grid gap-6">
                  <section className="surface-subtle p-5">
                    <p className="section-heading">Explanation</p>
                    <p className="mt-3 text-sm leading-7 text-slate-700">{device.explanation}</p>
                    {device.next_step ? (
                      <div className="mt-4 rounded-[16px] border border-[#d8dfea] bg-[#fafcff] px-4 py-4">
                        <p className="section-heading">Next step</p>
                        <p className="m-0 mt-2 text-sm leading-6 text-slate-700">{device.next_step}</p>
                      </div>
                    ) : null}
                  </section>

                  <section className="surface-subtle p-5">
                    <p className="section-heading">Telemetry</p>
                    {Object.entries(device.telemetry).length > 0 ? (
                      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        {Object.entries(device.telemetry).map(([key, value]) => (
                          <div key={key} className="rounded-[16px] border border-[#d8dfea] bg-white px-4 py-4">
                            <p className="m-0 text-[11px] font-semibold uppercase tracking-[0.08em] text-slate-500">{humanize(key)}</p>
                            <p className="m-0 mt-2 text-base font-semibold text-slate-950">{formatTelemetryValue(key, value)}</p>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="mt-4 rounded-[16px] border border-dashed border-[#d8dfea] bg-[#fafcff] px-4 py-4 text-sm text-slate-500">
                        No telemetry fields are available for this device yet.
                      </div>
                    )}
                  </section>
                </div>

                <div className="grid gap-6 self-start">
                  <section className="surface-subtle p-5">
                    <p className="section-heading">Identity</p>
                    <div className="mt-4 grid gap-3">
                      <div className="rounded-[16px] border border-[#d8dfea] bg-white px-4 py-4">
                        <p className="section-heading">Type</p>
                        <p className="m-0 mt-2 text-sm font-medium text-slate-900">{humanize(device.device_type)}</p>
                      </div>
                      <div className="rounded-[16px] border border-[#d8dfea] bg-white px-4 py-4">
                        <p className="section-heading">Last seen</p>
                        <p className="m-0 mt-2 text-sm font-medium text-slate-900">{formatDateTime(device.last_seen_at)}</p>
                      </div>
                      <div className="rounded-[16px] border border-[#d8dfea] bg-white px-4 py-4">
                        <p className="section-heading">Protocols</p>
                        <p className="m-0 mt-2 text-sm font-medium text-slate-900">
                          {device.protocols.length > 0 ? device.protocols.join(", ") : "None"}
                        </p>
                      </div>
                    </div>
                  </section>

                  <section className="surface-subtle p-5">
                    <p className="section-heading">Capabilities</p>
                    <div className="mt-4 flex flex-wrap gap-2">
                      <CapabilityPill label="Visible" enabled={device.capabilities.visible} />
                      <CapabilityPill label="Telemetry" enabled={device.capabilities.monitorable} />
                      <CapabilityPill label="Control" enabled={device.capabilities.controllable} />
                      <CapabilityPill label="Optimizable" enabled={device.capabilities.optimizable} />
                    </div>
                    {uiState.highlightedDeviceIds.includes(device.id) ? (
                      <div className="mt-4 rounded-[16px] border border-[#f1d7a2] bg-[#fff7e8] px-4 py-3 text-sm text-[#9c6410]">
                        This device is currently highlighted by the agent conversation.
                      </div>
                    ) : null}
                  </section>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    );
  }

  function renderCanvasAlerts() {
    if (!error && !notice && !uiState.explanation) {
      return null;
    }

    return (
      <div className="pointer-events-auto absolute left-1/2 top-5 z-20 flex w-[min(720px,calc(100%-2rem))] -translate-x-1/2 flex-col gap-3">
        {error ? <div className="rounded-[16px] border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        {notice ? <div className="rounded-[16px] border border-[#f1d7a2] bg-[#fff7e8] px-4 py-3 text-sm text-[#9c6410]">{notice}</div> : null}
        {uiState.explanation ? (
          <div className={`rounded-[16px] border px-4 py-3 text-sm ${severityTone(uiState.explanation.severity)}`}>
            <p className="m-0 font-semibold">{uiState.explanation.title}</p>
            <p className="m-0 mt-1 leading-6">{uiState.explanation.body}</p>
          </div>
        ) : null}
      </div>
    );
  }

  function renderOverviewCanvas() {
    return (
      <section className="relative h-full min-h-0 overflow-hidden border border-[#d8dfea] bg-[linear-gradient(180deg,#ffffff_0%,#f8fbff_100%)] shadow-[0_24px_64px_rgba(15,23,42,0.08)]">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_46%,rgba(255,220,150,0.28),transparent_18%),radial-gradient(circle_at_18%_16%,rgba(210,223,242,0.34),transparent_24%),radial-gradient(circle_at_84%_18%,rgba(255,233,190,0.24),transparent_22%)]" />
        {renderCanvasAlerts()}

        <div className="absolute inset-0">
          <div className="absolute left-1/2 top-1/2 z-10 w-[220px] -translate-x-1/2 -translate-y-1/2 rounded-[32px] border border-[#e5c88a] bg-[radial-gradient(circle_at_top,rgba(255,237,194,0.92),rgba(255,255,255,0.98)_62%)] px-6 py-7 text-center shadow-[0_28px_60px_rgba(212,163,74,0.18)]">
            <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-[#d88b08] text-white shadow-[0_16px_30px_rgba(216,139,8,0.3)]">
              <AppIcon name="home" className="h-7 w-7" />
            </div>
            <p className="m-0 mt-5 text-xl font-semibold text-slate-950">Home</p>
            <p className="m-0 mt-2 text-sm text-slate-600">
              {allDevices.length > 0 ? `${allDevices.length} detected device${allDevices.length === 1 ? "" : "s"}` : "Waiting for discovery"}
            </p>
          </div>

          {allDevices.map((device, index) => {
            const point = canvasPoints[index] ?? { x: 0, y: 0 };
            const selected = inspectedDeviceId === device.id;
            const highlighted =
              uiState.highlightedDeviceIds.includes(device.id) || uiState.selectedDeviceIds.includes(device.id);
            const matchesFocus = deviceMatchesFocusedSystem(device, uiState.focusedSystem);

            return (
              <button
                key={device.id}
                type="button"
                title={deviceSpecTooltip(device)}
                className={`absolute z-[5] w-[180px] -translate-x-1/2 -translate-y-1/2 rounded-[24px] border px-4 py-4 text-left transition duration-200 ${
                  selected
                    ? "border-[#d08a11] bg-[#fff9ee] shadow-[0_22px_40px_rgba(208,138,17,0.22)]"
                    : highlighted
                      ? "border-[#e7b14d] bg-[#fff9ef] shadow-[0_18px_34px_rgba(226,177,77,0.16)]"
                      : "border-[#d8dfea] bg-white/96 shadow-[0_16px_30px_rgba(15,23,42,0.08)] hover:border-[#bcc9de] hover:shadow-[0_18px_34px_rgba(15,23,42,0.12)]"
                } ${matchesFocus ? "opacity-100" : "opacity-50"}`}
                style={{
                  left: `calc(50% + ${point.x}px)`,
                  top: `calc(50% + ${point.y}px)`,
                }}
                onClick={() => setInspectedDeviceId(device.id)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[14px] border border-[#f1d7a2] bg-[#fff7e8] text-[#c47d0f]">
                    <AppIcon name={deviceIcon(device.device_type)} className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <p className="m-0 truncate text-sm font-semibold text-slate-950" title={device.name}>
                      {device.name}
                    </p>
                    <p className="m-0 mt-1 truncate text-xs text-slate-500">
                      {device.manufacturer}
                    </p>
                  </div>
                </div>
                <div className="mt-4 flex items-center justify-between gap-3">
                  <span className="truncate text-xs text-slate-600">{deviceTelemetrySummary(device)}</span>
                </div>
              </button>
            );
          })}

          {allDevices.length === 0 ? (
            <div className="absolute left-1/2 top-[calc(50%+170px)] z-10 w-[320px] -translate-x-1/2 rounded-[24px] border border-dashed border-[#d8dfea] bg-white/90 px-5 py-4 text-center text-sm leading-6 text-slate-500 shadow-[0_16px_34px_rgba(15,23,42,0.06)]">
              No devices have been detected yet. Ask Helios to scan the house, or configure the provider and site scope in Settings.
            </div>
          ) : null}
        </div>

        {inspectedDevice ? renderDeviceOverlay(inspectedDevice) : null}
      </section>
    );
  }

  function renderSettingsView() {
    return (
      <div className="subtle-scrollbar h-full min-h-0 overflow-y-auto pr-1">
        <div className="mx-auto flex max-w-[920px] flex-col gap-5">
          {(error || notice || uiState.explanation) ? (
            <div className="flex flex-col gap-3">
              {error ? <div className="rounded-[16px] border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
              {notice ? <div className="rounded-[16px] border border-[#f1d7a2] bg-[#fff7e8] px-4 py-3 text-sm text-[#9c6410]">{notice}</div> : null}
              {uiState.explanation ? (
                <div className={`rounded-[16px] border px-4 py-3 text-sm ${severityTone(uiState.explanation.severity)}`}>
                  <p className="m-0 font-semibold">{uiState.explanation.title}</p>
                  <p className="m-0 mt-1 leading-6">{uiState.explanation.body}</p>
                </div>
              ) : null}
            </div>
          ) : null}

          <ProviderFormSection
            agentProviderConfig={agentProviderConfig}
            providerForm={providerForm}
            selectedProviderOption={selectedProviderOption}
            providerReadyForChat={providerReadyForChat}
            busyAction={busyAction}
            onProviderSelection={(providerId) => {
              if (agentProviderConfig) {
                syncProviderForm(agentProviderConfig, providerId);
              }
            }}
            onProviderFormChange={(next) => setProviderForm((current) => ({ ...current, ...next }))}
            onSave={() => void handleProviderSave()}
            onClearKey={() => void handleProviderKeyClear()}
          />

          <section className="surface-subtle p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="section-heading">Networks</p>
                <p className="mt-2 text-sm leading-6 text-slate-600">
                  Helios only scans within the configured site scope. Apply the reachable networks from this machine or let the agent propose scope changes.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void handleUseReachableNetworks()}
                  disabled={busyAction !== null || reachableSubnets.length === 0}
                >
                  Use reachable networks
                </button>
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void handleRunDiscovery()}
                  disabled={busyAction !== null}
                >
                  Run discovery
                </button>
              </div>
            </div>

            <div className="mt-5 grid gap-4 md:grid-cols-2">
              <div className="rounded-[18px] border border-[#d8dfea] bg-white px-4 py-4">
                <p className="section-heading">Configured scope</p>
                {currentScope.length === 0 ? (
                  <p className="mt-3 text-sm leading-6 text-slate-500">No site scope has been configured yet.</p>
                ) : (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {currentScope.map((scope) => (
                      <span key={scope} className="rounded-full border border-[#d8dfea] bg-[#f8fbff] px-3 py-1.5 text-xs font-medium text-slate-700">
                        {scope}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <div className="rounded-[18px] border border-[#d8dfea] bg-white px-4 py-4">
                <p className="section-heading">Reachable networks</p>
                {reachableSubnets.length === 0 ? (
                  <p className="mt-3 text-sm leading-6 text-slate-500">No reachable networks are available on this machine.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {reachableSubnets.map((subnet) => (
                      <div key={`${subnet.cidr}-${subnet.interface}`} className="rounded-[14px] border border-[#d8dfea] bg-[#f8fbff] px-3 py-3">
                        <p className="m-0 text-sm font-semibold text-slate-900">{subnet.cidr}</p>
                        <p className="m-0 mt-1 text-xs text-slate-500">
                          {subnet.label} · {subnet.interface}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </section>

          <section className="surface-subtle p-5">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="rounded-[18px] border border-[#d8dfea] bg-white px-4 py-4">
                <p className="section-heading">Confirmed systems</p>
                {confirmedSystems.length === 0 ? (
                  <p className="mt-3 text-sm leading-6 text-slate-500">No systems have been confirmed yet.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {confirmedSystems.map((system) => (
                      <div key={system.label} className="rounded-[14px] border border-[#d8dfea] bg-[#f8fbff] px-3 py-3">
                        <p className="m-0 text-sm font-semibold text-slate-900">{system.label}</p>
                        <p className="m-0 mt-1 text-xs text-slate-500">{system.device_name ?? "Device not linked"}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="rounded-[18px] border border-[#d8dfea] bg-white px-4 py-4">
                <p className="section-heading">Open setup questions</p>
                {unresolvedItems.length === 0 ? (
                  <p className="mt-3 text-sm leading-6 text-slate-500">Helios currently has no unresolved setup questions.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {unresolvedItems.map((item) => (
                      <div key={`${item.kind}-${item.label}`} className="rounded-[14px] border border-[#d8dfea] bg-[#f8fbff] px-3 py-3">
                        <p className="m-0 text-sm font-semibold text-slate-900">{humanize(item.label)}</p>
                        <p className="m-0 mt-1 text-sm leading-6 text-slate-600">{item.details || "Needs confirmation."}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>
      </div>
    );
  }

  function renderChatPane() {
    if (!providerReadyForChat) {
      return (
        <div className="subtle-scrollbar h-full overflow-y-auto p-6">
          <ProviderFormSection
            agentProviderConfig={agentProviderConfig}
            providerForm={providerForm}
            selectedProviderOption={selectedProviderOption}
            providerReadyForChat={providerReadyForChat}
            busyAction={busyAction}
            onProviderSelection={(providerId) => {
              if (agentProviderConfig) {
                syncProviderForm(agentProviderConfig, providerId);
              }
            }}
            onProviderFormChange={(next) => setProviderForm((current) => ({ ...current, ...next }))}
            onSave={() => void handleProviderSave()}
            onClearKey={() => void handleProviderKeyClear()}
          />
        </div>
      );
    }

    return (
      <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_auto]">
        <div ref={timelineRef} className="subtle-scrollbar min-h-0 space-y-3 overflow-y-auto px-5 py-5">
          {timelineMessages.length === 0 ? (
            <div className="rounded-[18px] border border-[#d8dfea] bg-white px-4 py-4">
              <p className="m-0 text-sm font-medium text-slate-900">Helios is ready.</p>
              <p className="m-0 mt-2 text-sm leading-6 text-slate-600">
                Ask Helios to scan the house, inspect a device, or clarify what the current setup is missing.
              </p>
            </div>
          ) : (
            timelineMessages.map((message) => (
              <article key={message.id} className={`rounded-[18px] border px-4 py-4 ${messageCardClasses(message.role)}`}>
                <div className="mb-3 flex items-center justify-between gap-4 text-[11px] font-semibold uppercase tracking-[0.08em] text-slate-500">
                  <span>{message.role === "user" ? "You" : "Helios"}</span>
                  <span>{formatDateTime(message.created_at)}</span>
                </div>
                <div className="message-markdown text-slate-700">
                  {message.content ? (
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
                      }}
                    >
                      {message.content}
                    </ReactMarkdown>
                  ) : (
                    <p>…</p>
                  )}
                </div>
              </article>
            ))
          )}

          {thread?.pending_proposals.length ? (
            <div className="space-y-3">
              {thread.pending_proposals.map((proposal) => (
                <article key={proposal.id} className="rounded-[18px] border border-[#f1d7a2] bg-[#fffaf0] px-4 py-4">
                  <p className="section-heading">Confirmation needed</p>
                  <p className="mt-2 text-sm font-semibold text-slate-950">{proposal.summary}</p>
                  <p className="mt-2 text-sm leading-6 text-slate-600">{renderProposalSummary(proposal)}</p>
                  <div className="mt-4 flex items-center gap-2">
                    <button
                      type="button"
                      className="primary-button"
                      onClick={() => void handleProposalDecision(proposal.id, "confirm")}
                      disabled={busyAction !== null}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => void handleProposalDecision(proposal.id, "reject")}
                      disabled={busyAction !== null}
                    >
                      Reject
                    </button>
                  </div>
                </article>
              ))}
            </div>
          ) : null}
        </div>

        <div className="border-t border-[#d8dfea] px-4 py-4">
          <form className="w-full" onSubmit={handleSubmit}>
            <div className="flex w-full overflow-hidden rounded-[14px] border border-[#d8dfea] bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.7)]">
              <textarea
                className="block min-h-[120px] w-full flex-1 resize-none border-0 bg-transparent px-4 py-3 text-sm text-slate-900 outline-none"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => void handleComposerKeyDown(event)}
                placeholder="Ask Helios about your home energy setup…"
                disabled={activeTurnId !== null || busyAction === "send-message"}
              />
              <div className="flex w-[72px] shrink-0 items-end justify-center border-l border-[#d8dfea] p-3">
                <button
                  type="submit"
                  className="primary-button h-11 w-11 rounded-full px-0"
                  disabled={!draft.trim() || activeTurnId !== null || busyAction === "send-message"}
                  aria-label="Send message"
                >
                  <AppIcon name="send" className="h-4 w-4" />
                </button>
              </div>
            </div>
          </form>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center bg-[color:var(--hh-bg)]">
        <div className="surface-panel flex items-center gap-4 px-6 py-5">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[#fff0ce] text-[#d88b08]">
            <AppIcon name="sun" className="h-5 w-5" />
          </div>
          <p className="m-0 text-base font-semibold text-slate-900">Loading Helios Home…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full overflow-hidden bg-[color:var(--hh-bg)] text-slate-900">
      <aside
        className={`shrink-0 overflow-hidden border-r border-[#d8dfea] bg-white transition-[width] duration-200 ${
          navExpanded ? "w-[248px] cursor-default" : "w-[76px] cursor-e-resize"
        }`}
        onClick={() => {
          if (!navExpanded) {
            setNavExpanded(true);
          }
        }}
      >
        <div className="flex h-full min-h-0 flex-col">
          <div className={`flex items-center border-b border-[#d8dfea] py-5 ${navExpanded ? "justify-between px-5" : "justify-center px-3"}`}>
            {navExpanded ? (
              <div className="flex min-w-0 items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#fff0ce] text-[#d88b08]">
                  <AppIcon name="sun" className="h-5 w-5" />
                </div>
                <p className="m-0 truncate text-base font-semibold text-slate-950">Helios Home</p>
              </div>
            ) : (
              <button
                type="button"
                className="group relative flex h-10 w-10 items-center justify-center rounded-full bg-[#fff0ce] text-[#d88b08]"
                onClick={(event) => {
                  event.stopPropagation();
                  setNavExpanded(true);
                }}
                aria-label="Expand menu"
              >
                <AppIcon name="sun" className="h-5 w-5 transition-opacity duration-150 group-hover:opacity-0" />
                <AppIcon name="chevron-right" className="absolute h-5 w-5 opacity-0 transition-opacity duration-150 group-hover:opacity-100" />
              </button>
            )}
            <button
              type="button"
              className={`secondary-button ${navExpanded ? "h-10 w-10 shrink-0 px-0" : "absolute left-1/2 top-5 hidden -translate-x-1/2"}`}
              onClick={(event) => {
                event.stopPropagation();
                setNavExpanded((current) => !current);
              }}
              aria-label={navExpanded ? "Collapse menu" : "Expand menu"}
            >
              <AppIcon name={navExpanded ? "chevron-left" : "chevron-right"} className="h-4 w-4" />
            </button>
          </div>

          <nav className={`flex-1 py-5 ${navExpanded ? "px-4" : "px-3"}`}>
            <div className="grid gap-1">
              {NAV_ITEMS.map((item) => {
                const isActive = currentView === item.view;
                return (
                  <button
                    key={item.view}
                    type="button"
                    className={`flex h-11 items-center rounded-[14px] text-sm font-medium transition ${
                      navExpanded ? "gap-3 px-3" : "justify-center px-0"
                    } ${
                      isActive
                        ? "bg-[#fff5de] text-[#a56614] shadow-[0_12px_28px_rgba(212,139,8,0.12)]"
                        : "text-slate-600 hover:bg-[#f8fbff] hover:text-slate-950"
                    }`}
                    onClick={(event) => {
                      event.stopPropagation();
                      dispatchUiState({ type: "set_view", view: item.view });
                    }}
                  >
                    <AppIcon name={item.icon} className="h-4 w-4 shrink-0" />
                    {navExpanded ? <span>{item.label}</span> : null}
                  </button>
                );
              })}
            </div>
          </nav>
        </div>
      </aside>

      <div className="relative grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)_420px]">
        <main className={`min-h-0 min-w-0 overflow-hidden ${currentView === "overview" ? "p-0" : "p-5"}`}>
          {currentView === "settings" ? renderSettingsView() : renderOverviewCanvas()}
        </main>

        <aside className="min-h-0 border-l border-[#d8dfea] bg-[linear-gradient(180deg,#ffffff_0%,#f8fbff_100%)]">
          {renderChatPane()}
        </aside>
      </div>
    </div>
  );
}
