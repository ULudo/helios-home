import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { CapabilityPill } from "./components/CapabilityPill";
import { StatusBadge } from "./components/StatusBadge";
import { api } from "./lib/api";
import type {
  ActionProposalRead,
  AgentMessageRead,
  AgentProviderConfigRead,
  AgentProviderOptionRead,
  AgentThreadRead,
  AgentTurnEventRead,
  DeviceRead,
  HemsPlanRead,
  HemsSummaryRead,
  OverviewResponse,
  ReachableSubnetRead,
} from "./lib/types";

type ActivityEntry = {
  id: string;
  tone: "neutral" | "positive" | "critical";
  title: string;
  detail: string;
  createdAt: string;
};

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

function trimMessagePreview(value: string, maxLength = 180): string {
  const normalized = value.trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 1)}…`;
}

function activityFromEvent(event: AgentTurnEventRead): ActivityEntry | null {
  if (event.event_type === "tool_started") {
    return {
      id: `${event.turn_id}-${event.event_type}-${event.created_at}`,
      tone: "neutral",
      title: `Running ${humanize(String(event.payload.tool_name ?? "tool"))}`,
      detail: "Helios is gathering more context.",
      createdAt: event.created_at,
    };
  }
  if (event.event_type === "tool_finished") {
    return {
      id: `${event.turn_id}-${event.event_type}-${event.created_at}`,
      tone: "positive",
      title: `${humanize(String(event.payload.tool_name ?? "tool"))} finished`,
      detail: trimMessagePreview(JSON.stringify(event.payload.result ?? {}), 140),
      createdAt: event.created_at,
    };
  }
  if (event.event_type === "proposal_created") {
    return {
      id: `${event.turn_id}-${event.event_type}-${event.created_at}`,
      tone: "neutral",
      title: "Confirmation needed",
      detail: String(event.payload.summary ?? "Helios prepared the next setup action."),
      createdAt: event.created_at,
    };
  }
  if (event.event_type === "error") {
    return {
      id: `${event.turn_id}-${event.event_type}-${event.created_at}`,
      tone: "critical",
      title: "Agent turn failed",
      detail: String(event.payload.message ?? "Unknown error."),
      createdAt: event.created_at,
    };
  }
  return null;
}

function messageTone(role: string): string {
  if (role === "user") {
    return "message-user";
  }
  if (role === "assistant") {
    return "message-assistant";
  }
  return "message-system";
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

export default function App() {
  const [thread, setThread] = useState<AgentThreadRead | null>(null);
  const [agentProviderConfig, setAgentProviderConfig] = useState<AgentProviderConfigRead | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [reachableSubnets, setReachableSubnets] = useState<ReachableSubnetRead[]>([]);
  const [hemsSummary, setHemsSummary] = useState<HemsSummaryRead | null>(null);
  const [hemsPlan, setHemsPlan] = useState<HemsPlanRead | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [activities, setActivities] = useState<ActivityEntry[]>([]);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [streamingAssistant, setStreamingAssistant] = useState<AgentMessageRead | null>(null);
  const [providerForm, setProviderForm] = useState({
    providerId: "stub",
    model: "",
    baseUrl: "",
    apiKey: "",
  });
  const streamRef = useRef<EventSource | null>(null);
  const timelineRef = useRef<HTMLDivElement | null>(null);

  const currentScope = useMemo(
    () => parseConfiguredSubnets(overview?.site.local_subnet ?? ""),
    [overview?.site.local_subnet],
  );

  const timelineMessages = useMemo(() => {
    const persisted = thread?.messages ?? [];
    if (streamingAssistant === null) {
      return persisted;
    }
    return [...persisted, streamingAssistant];
  }, [thread?.messages, streamingAssistant]);

  const selectedProviderOption = useMemo<AgentProviderOptionRead | null>(() => {
    if (agentProviderConfig === null) {
      return null;
    }
    return (
      agentProviderConfig.provider_options.find((option) => option.provider_id === providerForm.providerId) ??
      agentProviderConfig.provider_options.find((option) => option.selected) ??
      null
    );
  }, [agentProviderConfig, providerForm.providerId]);

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
    const [nextThread, nextProviderConfig, nextOverview, nextSubnets, nextHemsSummary, nextPlan] = await Promise.all([
      api.getAgentThread(),
      api.getAgentProviderConfig(),
      api.getOverview(),
      api.listReachableSubnets(),
      api.getHemsSummary(),
      api.getLatestHemsPlan(),
    ]);
    setThread(nextThread);
    setAgentProviderConfig(nextProviderConfig);
    setOverview(nextOverview);
    setReachableSubnets(nextSubnets);
    setHemsSummary(nextHemsSummary);
    setHemsPlan(nextPlan);
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
  }, [timelineMessages, activities]);

  function startStream(turnId: string) {
    streamRef.current?.close();
    streamRef.current = api.streamAgentTurn(turnId, {
      onEvent: (event) => {
        if (event.event_type === "assistant_delta") {
          const delta = String(event.payload.delta ?? "");
          setStreamingAssistant((current) => {
            if (current === null) {
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
          if (nextMessage) {
            setThread((current) => {
              if (current === null) {
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

        const nextActivity = activityFromEvent(event);
        if (nextActivity !== null) {
          setActivities((current) => [nextActivity, ...current].slice(0, 16));
        }

        if (event.event_type === "proposal_created") {
          setNotice("Helios prepared the next setup action for confirmation.");
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
    setBusyAction("send-message");
    setError(null);
    setNotice(null);
    try {
      const accepted = await api.createAgentMessage({ content: normalized });
      setThread((current) => {
        if (current === null) {
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
      setNotice(
        decision === "confirm"
          ? "Helios applied your confirmation."
          : "Helios left the current setup unchanged.",
      );
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

  if (loading) {
    return (
      <div className="shell loading-shell">
        <div className="loading-card">
          <span className="solar-mark" aria-hidden="true">
            ☼
          </span>
          <p>Loading Helios Home…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="shell-sidebar">
        <div className="brand-lockup">
          <span className="solar-mark" aria-hidden="true">
            ☼
          </span>
          <div>
            <h1>Helios Home</h1>
            <p>Agent-first setup for your home's energy systems.</p>
          </div>
        </div>

        <section className="sidebar-panel">
          <div className="panel-heading">
            <h2>Setup progress</h2>
            <span>{thread?.setup_profile.confirmed_systems.length ?? 0} confirmed</span>
          </div>
          <p className="panel-copy">{thread?.setup_profile.summary ?? "No setup state yet."}</p>
          <div className="chip-row">
            {(thread?.setup_profile.confirmed_systems ?? []).map((binding) => (
              <span key={`${binding.system_type}-${binding.device_id ?? binding.label}`} className="system-chip">
                {humanize(binding.system_type)} · {binding.label}
              </span>
            ))}
          </div>
          {(thread?.setup_profile.unresolved_items ?? []).length > 0 ? (
            <ul className="unresolved-list">
              {thread?.setup_profile.unresolved_items.map((item) => (
                <li key={`${item.kind}-${item.label}`}>
                  <strong>{humanize(item.label)}</strong>
                  <span>{item.details || "Needs confirmation."}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        <section className="sidebar-panel">
          <div className="panel-heading">
            <h2>Quick actions</h2>
          </div>
          <div className="quick-action-grid">
            <button type="button" onClick={() => void sendMessage("Scan the house and tell me what you find.")}>
              Scan the house
            </button>
            <button type="button" onClick={() => void sendMessage("What do you see right now?")}>
              Summarize devices
            </button>
            <button type="button" onClick={() => void sendMessage("I want to integrate my heat pump.")}>
              Integrate heat pump
            </button>
            <button type="button" onClick={() => void sendMessage("Use all networks for discovery.")}>
              Use all networks
            </button>
          </div>
        </section>

        <section className="sidebar-panel">
          <div className="panel-heading">
            <h2>AI provider</h2>
            <span>{agentProviderConfig?.effective_provider === "stub" ? "Stub" : "Ready"}</span>
          </div>
          <p className="panel-copy">{agentProviderConfig?.message ?? "Configure a provider for model-backed responses."}</p>
          <div className="provider-config-grid">
            <label className="field-stack">
              <span>Provider</span>
              <select
                value={providerForm.providerId}
                onChange={(event) => syncProviderForm(agentProviderConfig!, event.target.value)}
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
              <label className="field-stack">
                <span>Model</span>
                <input
                  type="text"
                  value={providerForm.model}
                  placeholder={selectedProviderOption.model_placeholder}
                  onChange={(event) =>
                    setProviderForm((current) => ({
                      ...current,
                      model: event.target.value,
                    }))
                  }
                  disabled={busyAction !== null}
                />
              </label>
            ) : null}

            {selectedProviderOption?.supports_base_url ? (
              <label className="field-stack">
                <span>Base URL</span>
                <input
                  type="text"
                  value={providerForm.baseUrl}
                  placeholder={selectedProviderOption.base_url_default ?? ""}
                  onChange={(event) =>
                    setProviderForm((current) => ({
                      ...current,
                      baseUrl: event.target.value,
                    }))
                  }
                  disabled={busyAction !== null}
                />
              </label>
            ) : null}

            {selectedProviderOption?.auth_kind === "api_key" ? (
              <label className="field-stack">
                <span>API key</span>
                <input
                  type="password"
                  value={providerForm.apiKey}
                  placeholder={selectedProviderOption.api_key_configured ? "Stored locally. Leave blank to keep it." : "Paste the key once"}
                  onChange={(event) =>
                    setProviderForm((current) => ({
                      ...current,
                      apiKey: event.target.value,
                    }))
                  }
                  disabled={busyAction !== null}
                />
              </label>
            ) : null}

            <div className="provider-actions">
              <button type="button" onClick={() => void handleProviderSave()} disabled={busyAction !== null}>
                Save provider
              </button>
              {selectedProviderOption?.auth_kind === "api_key" && selectedProviderOption.api_key_configured ? (
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void handleProviderKeyClear()}
                  disabled={busyAction !== null}
                >
                  Clear key
                </button>
              ) : null}
            </div>

            <p className="field-hint">Provider credentials stay on this machine and are not returned by the API after saving.</p>
          </div>
        </section>

        <section className="sidebar-panel">
          <div className="panel-heading">
            <h2>Reachable networks</h2>
            <span>{reachableSubnets.length}</span>
          </div>
          <div className="subnet-list">
            {reachableSubnets.map((subnet) => {
              const selected = currentScope.includes(subnet.cidr);
              return (
                <div key={subnet.cidr} className={`subnet-card ${selected ? "selected" : ""}`}>
                  <div>
                    <strong>{subnet.cidr}</strong>
                    <span>{subnet.label}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => void sendMessage(`Use ${subnet.cidr} for discovery.`)}
                    disabled={activeTurnId !== null}
                  >
                    {selected ? "Selected" : "Ask Helios"}
                  </button>
                </div>
              );
            })}
          </div>
        </section>

        <section className="sidebar-panel">
          <div className="panel-heading">
            <h2>Detected devices</h2>
            <span>{overview?.devices.length ?? 0}</span>
          </div>
          <ul className="device-preview-list">
            {(overview?.devices ?? []).slice(0, 6).map((device) => (
              <li key={device.id}>
                <div className="truncate-stack">
                  <strong className="truncate-line" title={device.name}>
                    {device.name}
                  </strong>
                  <span className="truncate-line" title={humanize(device.device_type)}>
                    {humanize(device.device_type)}
                  </span>
                </div>
                <StatusBadge status={device.primary_status} />
              </li>
            ))}
          </ul>
        </section>

        <footer className="shell-footer">© 2026 NeurHelios</footer>
      </aside>

      <main className="conversation-shell">
        <header className="workspace-header">
          <div>
            <span className="eyebrow">Primary workspace</span>
            <h2>Talk to Helios</h2>
            <p>Describe what you want to set up. Helios will inspect the house, ask targeted follow-up questions, and prepare the next safe action.</p>
          </div>
          <div className="header-actions">
            {activeTurnId ? <span className="live-indicator">Live</span> : null}
            <button type="button" className="secondary-button" onClick={() => setAdvancedOpen(true)}>
              Advanced
            </button>
          </div>
        </header>

        {error ? <div className="banner banner-error">{error}</div> : null}
        {notice ? <div className="banner banner-notice">{notice}</div> : null}

        <div className="workspace-grid">
          <section className="timeline-panel">
            <div ref={timelineRef} className="timeline">
              {timelineMessages.map((message) => (
                <article key={message.id} className={`message-card ${messageTone(message.role)}`}>
                  <div className="message-meta">
                    <span>{message.role === "user" ? "You" : "Helios"}</span>
                    <span>{formatDateTime(message.created_at)}</span>
                  </div>
                  <div className="message-body">
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
              ))}
            </div>

            <form className="composer" onSubmit={(event) => void handleSubmit(event)}>
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="Example: I want to connect my heat pump and I am not sure which device it is."
                rows={3}
                disabled={activeTurnId !== null}
              />
              <div className="composer-actions">
                <span className="composer-hint">Read actions run automatically. State changes still require your confirmation.</span>
                <button type="submit" disabled={!draft.trim() || activeTurnId !== null || busyAction === "send-message"}>
                  {activeTurnId ? "Helios is working…" : "Send"}
                </button>
              </div>
            </form>
          </section>

          <aside className="helper-rail">
            <section className="helper-panel">
              <div className="panel-heading">
                <h3>Pending actions</h3>
                <span>{thread?.pending_proposals.length ?? 0}</span>
              </div>
              {(thread?.pending_proposals ?? []).length === 0 ? (
                <p className="empty-copy">No confirmation is waiting right now.</p>
              ) : (
                <div className="proposal-list">
                  {thread?.pending_proposals.map((proposal) => (
                    <article key={proposal.id} className="proposal-card">
                      <div className="proposal-copy">
                        <strong>{proposal.summary}</strong>
                        <p>{renderProposalSummary(proposal)}</p>
                      </div>
                      <div className="proposal-actions">
                        <button
                          type="button"
                          onClick={() => void handleProposalDecision(proposal.id, "confirm")}
                          disabled={busyAction !== null}
                        >
                          Confirm
                        </button>
                        <button
                          type="button"
                          className="ghost-button"
                          onClick={() => void handleProposalDecision(proposal.id, "reject")}
                          disabled={busyAction !== null}
                        >
                          Reject
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </section>

            <section className="helper-panel">
              <div className="panel-heading">
                <h3>Live activity</h3>
              </div>
              {activities.length === 0 ? (
                <p className="empty-copy">Activity from the current turn will appear here while Helios works.</p>
              ) : (
                <ul className="activity-list">
                  {activities.map((activity) => (
                    <li key={activity.id} className={`activity-item ${activity.tone}`}>
                      <strong>{activity.title}</strong>
                      <span>{activity.detail}</span>
                      <small>{formatDateTime(activity.createdAt)}</small>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {thread?.latest_debug_case ? (
              <section className="helper-panel">
                <div className="panel-heading">
                  <h3>Latest investigation</h3>
                </div>
                <strong>{thread.latest_debug_case.subject_label}</strong>
                <p>{thread.latest_debug_case.diagnosis.summary}</p>
              </section>
            ) : null}
          </aside>
        </div>
      </main>

      {advancedOpen ? (
        <div className="advanced-backdrop" onClick={() => setAdvancedOpen(false)}>
          <aside className="advanced-drawer" onClick={(event) => event.stopPropagation()}>
            <div className="advanced-header">
              <div>
                <span className="eyebrow">Advanced</span>
                <h3>Technical runtime state</h3>
              </div>
              <button type="button" className="ghost-button" onClick={() => setAdvancedOpen(false)}>
                Close
              </button>
            </div>

            <section className="advanced-section">
              <div className="panel-heading">
                <h4>Inventory</h4>
                <span>{overview?.devices.length ?? 0}</span>
              </div>
              <div className="advanced-device-list">
                {(overview?.devices ?? []).map((device) => (
                  <article key={device.id} className="advanced-device-card">
                    <div className="advanced-device-header">
                      <div className="truncate-stack">
                        <strong className="truncate-line" title={device.name}>
                          {device.name}
                        </strong>
                        <span className="truncate-line" title={`${device.manufacturer} · ${device.model}`}>
                          {device.manufacturer} · {device.model}
                        </span>
                      </div>
                      <StatusBadge status={device.primary_status} />
                    </div>
                    <div className="capability-row">
                      <CapabilityPill enabled={device.capabilities.visible} label="Visible" />
                      <CapabilityPill enabled={device.capabilities.monitorable} label="Telemetry" />
                      <CapabilityPill enabled={device.capabilities.controllable} label="Control" />
                      <CapabilityPill enabled={device.capabilities.optimizable} label="Optimizable" />
                    </div>
                    <p>{device.explanation}</p>
                    <small>Next step: {device.next_step || "None"}</small>
                  </article>
                ))}
              </div>
            </section>

            <section className="advanced-section">
              <div className="panel-heading">
                <h4>HEMS runtime</h4>
              </div>
              {hemsSummary ? (
                <div className="advanced-grid">
                  <div className="metric-card">
                    <span>Assets</span>
                    <strong>{hemsSummary.asset_count}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Dispatchable</span>
                    <strong>{hemsSummary.dispatchable_asset_count}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Plan only</span>
                    <strong>{hemsSummary.plan_only_asset_count}</strong>
                  </div>
                  <div className="metric-card">
                    <span>Blocked</span>
                    <strong>{hemsSummary.blocked_asset_count}</strong>
                  </div>
                </div>
              ) : null}
              {hemsPlan ? (
                <div className="plan-card">
                  <strong>{hemsPlan.summary}</strong>
                  <span>
                    {hemsPlan.intervals.length} intervals · {hemsPlan.dispatch_events.length} dispatch event(s)
                  </span>
                  <small>
                    Latest plan: {formatDateTime(hemsPlan.created_at)} · {humanize(hemsPlan.status)}
                  </small>
                </div>
              ) : (
                <p className="empty-copy">No HEMS plan has been generated yet.</p>
              )}
            </section>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
