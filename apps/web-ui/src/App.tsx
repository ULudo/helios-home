import { useEffect, useRef, useState, type ChangeEvent, type TouchEvent } from "react";

import { CapabilityPill } from "./components/CapabilityPill";
import { StatusBadge } from "./components/StatusBadge";
import { api } from "./lib/api";
import type {
  DeviceRead,
  HemsAssetRead,
  HemsDispatchEventRead,
  HemsPlanIntervalRead,
  HemsPlanRead,
  HemsPolicyRead,
  HemsSummaryRead,
  OverviewResponse,
  ReachableSubnetRead,
} from "./lib/types";

type PageKey = "devices" | "hems";
type DevicePanelView = "details" | "monitoring";

type TelemetrySample = {
  recordedAt: string;
  metrics: Record<string, number>;
};

const MAX_TELEMETRY_SAMPLES = 48;

function humanize(value: string): string {
  return value.split("_").join(" ");
}

function humanizeLabel(value: string): string {
  const text = humanize(value);
  return text.charAt(0).toUpperCase() + text.slice(1);
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

function formatValue(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value === "number") {
    return formatNumber(value);
  }
  return String(value);
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

function formatClockTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function formatTimeRange(startsAt: string, endsAt: string): string {
  return `${formatClockTime(startsAt)} - ${formatClockTime(endsAt)}`;
}

function parseConfiguredSubnets(rawValue: string): string[] {
  return rawValue
    .split(/[\n,;]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function serializeSubnets(subnets: string[]): string {
  return subnets.join(", ");
}

function toNumber(value: string | number | boolean): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function extractNumericTelemetry(device: DeviceRead): Record<string, number> {
  const metrics: Record<string, number> = {};
  Object.entries(device.telemetry).forEach(([key, value]) => {
    const numericValue = toNumber(value);
    if (numericValue !== null) {
      metrics[key] = numericValue;
    }
  });
  return metrics;
}

function appendTelemetrySamples(
  previous: Record<string, TelemetrySample[]>,
  devices: DeviceRead[],
): Record<string, TelemetrySample[]> {
  const nextHistory = { ...previous };

  devices.forEach((device) => {
    const metrics = extractNumericTelemetry(device);
    if (Object.keys(metrics).length === 0) {
      return;
    }

    const sample: TelemetrySample = {
      recordedAt: device.last_seen_at ?? new Date().toISOString(),
      metrics,
    };
    const existingSamples = nextHistory[device.id] ?? [];
    const lastSample = existingSamples[existingSamples.length - 1];

    if (lastSample && lastSample.recordedAt === sample.recordedAt) {
      return;
    }

    nextHistory[device.id] = [...existingSamples, sample].slice(-MAX_TELEMETRY_SAMPLES);
  });

  return nextHistory;
}

function metricKeysForDevice(device: DeviceRead, samples: TelemetrySample[]): string[] {
  const metricKeys = new Set<string>(Object.keys(extractNumericTelemetry(device)));
  samples.forEach((sample) => {
    Object.keys(sample.metrics).forEach((key) => metricKeys.add(key));
  });
  return Array.from(metricKeys).sort();
}

function buildChartGeometry(samples: TelemetrySample[], metricKey: string) {
  const width = 680;
  const height = 210;
  const paddingX = 16;
  const paddingTop = 12;
  const paddingBottom = 18;
  const values = samples
    .map((sample) => sample.metrics[metricKey])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));

  if (values.length === 0) {
    return null;
  }

  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const range = maxValue - minValue || 1;
  const stepX = values.length === 1 ? 0 : (width - paddingX * 2) / (values.length - 1);
  const chartHeight = height - paddingTop - paddingBottom;

  const points = values.map((value, index) => {
    const x = paddingX + stepX * index;
    const normalized = (value - minValue) / range;
    const y = paddingTop + (1 - normalized) * chartHeight;
    return { x, y, value };
  });

  const linePath = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(" ");
  const areaPath = `${linePath} L ${points[points.length - 1]?.x.toFixed(2)} ${(height - paddingBottom).toFixed(2)} L ${points[0]?.x.toFixed(2)} ${(height - paddingBottom).toFixed(2)} Z`;

  return {
    width,
    height,
    minValue,
    maxValue,
    points,
    linePath,
    areaPath,
  };
}

function summarizeEntries(
  values: Record<string, string | number | boolean>,
  limit = 4,
): Array<[string, string | number | boolean]> {
  return Object.entries(values).slice(0, limit);
}

function commandSummary(command: Record<string, string | number | boolean>): string {
  const entries = summarizeEntries(command, 3);
  if (entries.length === 0) {
    return "Idle";
  }
  return entries.map(([key, value]) => `${humanize(key)} ${formatValue(value)}`).join(" • ");
}

function toneForEligibility(eligibility: string): string {
  if (eligibility === "dispatchable") {
    return "tone-positive";
  }
  if (eligibility === "plan_only") {
    return "tone-caution";
  }
  if (eligibility === "blocked") {
    return "tone-critical";
  }
  return "tone-muted";
}

function toneForDispatchStatus(status: string): string {
  if (status === "applied") {
    return "tone-positive";
  }
  if (status === "simulated") {
    return "tone-neutral";
  }
  if (status === "blocked" || status === "failed") {
    return "tone-critical";
  }
  return "tone-caution";
}

function toneForViolationSeverity(severity: string): string {
  if (severity === "info") {
    return "tone-neutral";
  }
  if (severity === "warning") {
    return "tone-caution";
  }
  return "tone-critical";
}

function buildPolicyPayload(policy: HemsPolicyRead) {
  return {
    execution_mode: policy.execution_mode,
    battery_reserve_pct: policy.battery_reserve_pct,
    ev_default_target_soc_pct: policy.ev_default_target_soc_pct,
    ev_default_departure_time: policy.ev_default_departure_time,
    heat_comfort_min_c: policy.heat_comfort_min_c,
    heat_comfort_max_c: policy.heat_comfort_max_c,
    grid_import_limit_kw: policy.grid_import_limit_kw,
    grid_export_limit_kw: policy.grid_export_limit_kw,
    allow_price_arbitrage: policy.allow_price_arbitrage,
    allow_heat_precharge: policy.allow_heat_precharge,
    allow_ev_load_shifting: policy.allow_ev_load_shifting,
    horizon_hours: policy.horizon_hours,
    step_minutes: policy.step_minutes,
  };
}

function planIntervalsByAsset(plan: HemsPlanRead | null, assets: HemsAssetRead[]) {
  if (!plan) {
    return [];
  }

  const assetsByKey = new Map(assets.map((asset) => [asset.asset_key, asset]));
  const grouped = new Map<string, HemsPlanIntervalRead[]>();
  plan.intervals.forEach((interval) => {
    const current = grouped.get(interval.asset_key) ?? [];
    current.push(interval);
    grouped.set(interval.asset_key, current);
  });

  return Array.from(grouped.entries())
    .map(([assetKey, intervals]) => ({
      assetKey,
      asset: assetsByKey.get(assetKey) ?? null,
      intervals: intervals.sort((left, right) => left.starts_at.localeCompare(right.starts_at)),
    }))
    .sort((left, right) => left.assetKey.localeCompare(right.assetKey));
}

function MonitoringPanel({
  device,
  samples,
  selectedMetric,
  onSelectMetric,
}: {
  device: DeviceRead;
  samples: TelemetrySample[];
  selectedMetric: string | null;
  onSelectMetric: (metricKey: string) => void;
}) {
  const metrics = metricKeysForDevice(device, samples);
  const activeMetric = selectedMetric && metrics.includes(selectedMetric) ? selectedMetric : metrics[0] ?? null;
  const chart = activeMetric ? buildChartGeometry(samples, activeMetric) : null;

  if (metrics.length === 0 || activeMetric === null) {
    return (
      <div className="monitoring-panel">
        <div className="monitoring-header">
          <div>
            <h4>Monitoring</h4>
            <p className="inline-note">No numeric telemetry history has been recorded for this device yet.</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="monitoring-panel">
      <div className="monitoring-header">
        <div>
          <h4>Monitoring</h4>
          <p className="inline-note">Session-based history built from repeated refreshes and discovery runs.</p>
        </div>
        <div className="monitoring-stats">
          <span className="soft-tag">{humanize(activeMetric)}</span>
          <span className="soft-tag">{samples.length} samples</span>
        </div>
      </div>

      <div className="device-detail-tabs monitoring-metric-picker">
        {metrics.map((metricKey) => (
          <button
            key={metricKey}
            type="button"
            className={`metric-chip ${metricKey === activeMetric ? "active" : ""}`}
            onClick={() => onSelectMetric(metricKey)}
          >
            {humanize(metricKey)}
          </button>
        ))}
      </div>

      {chart ? (
        <>
          <svg className="monitoring-chart" viewBox={`0 0 ${chart.width} ${chart.height}`} preserveAspectRatio="none">
            <defs>
              <linearGradient id={`monitoring-fill-${device.id}`} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor="rgba(45, 120, 96, 0.24)" />
                <stop offset="100%" stopColor="rgba(45, 120, 96, 0.02)" />
              </linearGradient>
            </defs>
            <path
              className="monitoring-area"
              d={chart.areaPath}
              fill={`url(#monitoring-fill-${device.id})`}
            />
            <path className="monitoring-line" d={chart.linePath} fill="none" />
            {chart.points.map((point, index) => (
              <circle
                key={`${activeMetric}-${index}`}
                className="monitoring-dot"
                cx={point.x}
                cy={point.y}
                r="4"
              />
            ))}
          </svg>
          <div className="monitoring-footer">
            <span>
              Range {formatNumber(chart.minValue)} - {formatNumber(chart.maxValue)}
            </span>
            <span>
              Latest {formatValue(samples[samples.length - 1]?.metrics[activeMetric])} at{" "}
              {formatDateTime(samples[samples.length - 1]?.recordedAt)}
            </span>
          </div>
        </>
      ) : null}
    </div>
  );
}

export default function App() {
  const [currentPage, setCurrentPage] = useState<PageKey>("devices");
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [reachableSubnets, setReachableSubnets] = useState<ReachableSubnetRead[]>([]);
  const [selectedSubnets, setSelectedSubnets] = useState<string[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);
  const [devicePanelViews, setDevicePanelViews] = useState<Record<string, DevicePanelView>>({});
  const [deviceMetricSelection, setDeviceMetricSelection] = useState<Record<string, string>>({});
  const [telemetryHistory, setTelemetryHistory] = useState<Record<string, TelemetrySample[]>>({});
  const [hemsSummary, setHemsSummary] = useState<HemsSummaryRead | null>(null);
  const [hemsAssets, setHemsAssets] = useState<HemsAssetRead[]>([]);
  const [hemsPlan, setHemsPlan] = useState<HemsPlanRead | null>(null);
  const [policyDraft, setPolicyDraft] = useState<HemsPolicyRead | null>(null);
  const [policyDirty, setPolicyDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const touchStartX = useRef<Record<string, number>>({});

  async function refreshOverviewSnapshot(silent = false) {
    if (!silent) {
      setBusyAction("refresh-devices");
    }

    try {
      const nextOverview = await api.getOverview();
      setOverview(nextOverview);
      setSelectedSubnets(parseConfiguredSubnets(nextOverview.site.local_subnet));
      setTelemetryHistory((previous) => appendTelemetrySamples(previous, nextOverview.devices));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to refresh devices.");
    } finally {
      if (!silent) {
        setBusyAction(null);
      }
    }
  }

  async function refreshHemsData(options?: { preserveDraft?: boolean; silent?: boolean }) {
    if (!options?.silent) {
      setBusyAction("refresh-hems");
    }

    try {
      const [summary, assets, latestPlan] = await Promise.all([
        api.getHemsSummary(),
        api.listHemsAssets(),
        api.getLatestHemsPlan(),
      ]);
      setHemsSummary(summary);
      setHemsAssets(assets);
      setHemsPlan(latestPlan);
      if (!options?.preserveDraft || policyDraft === null || !policyDirty) {
        setPolicyDraft(summary.policy);
        setPolicyDirty(false);
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to refresh HEMS.");
    } finally {
      if (!options?.silent) {
        setBusyAction(null);
      }
    }
  }

  useEffect(() => {
    async function bootstrap() {
      setLoading(true);
      setError(null);
      try {
        const [nextOverview, nextSubnets, summary, assets, latestPlan] = await Promise.all([
          api.getOverview(),
          api.listReachableSubnets(),
          api.getHemsSummary(),
          api.listHemsAssets(),
          api.getLatestHemsPlan(),
        ]);

        setOverview(nextOverview);
        setReachableSubnets(nextSubnets);
        setSelectedSubnets(parseConfiguredSubnets(nextOverview.site.local_subnet));
        setTelemetryHistory((previous) => appendTelemetrySamples(previous, nextOverview.devices));
        setHemsSummary(summary);
        setHemsAssets(assets);
        setHemsPlan(latestPlan);
        setPolicyDraft(summary.policy);
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : "Unable to load Helios Home.");
      } finally {
        setLoading(false);
      }
    }

    void bootstrap();
  }, []);

  useEffect(() => {
    if (currentPage !== "devices") {
      return undefined;
    }

    const intervalId = window.setInterval(() => {
      void refreshOverviewSnapshot(true);
    }, 20000);

    return () => window.clearInterval(intervalId);
  }, [currentPage]);

  function toggleSubnet(cidr: string) {
    setSelectedSubnets((previous) =>
      previous.includes(cidr) ? previous.filter((entry) => entry !== cidr) : [...previous, cidr],
    );
  }

  async function handleDiscoveryRun() {
    if (overview === null) {
      return;
    }

    setBusyAction("run-discovery");
    setError(null);
    setNotice(null);

    try {
      const nextScope = serializeSubnets(selectedSubnets);
      if (nextScope !== overview.site.local_subnet) {
        await api.updateSite({ local_subnet: nextScope });
      }

      const discoveryRun = await api.runDiscovery();
      const [nextOverview, nextSummary, nextAssets, latestPlan] = await Promise.all([
        api.getOverview(),
        api.getHemsSummary(),
        api.listHemsAssets(),
        api.getLatestHemsPlan(),
      ]);

      setOverview(nextOverview);
      setSelectedSubnets(parseConfiguredSubnets(nextOverview.site.local_subnet));
      setTelemetryHistory((previous) => appendTelemetrySamples(previous, nextOverview.devices));
      setHemsSummary(nextSummary);
      setHemsAssets(nextAssets);
      setHemsPlan(latestPlan);
      if (!policyDirty) {
        setPolicyDraft(nextSummary.policy);
      }
      setNotice(
        `Discovery refreshed ${discoveryRun.refreshed_devices} devices and integrated ${discoveryRun.integrated_devices}.`,
      );
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to run discovery.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleRefreshDevices() {
    setError(null);
    setNotice(null);
    await Promise.all([refreshOverviewSnapshot(), api.listReachableSubnets().then(setReachableSubnets)]);
  }

  async function handleRefreshHems() {
    setError(null);
    setNotice(null);
    await refreshHemsData({ preserveDraft: true });
  }

  function updatePolicyField<K extends keyof HemsPolicyRead>(field: K, value: HemsPolicyRead[K]) {
    setPolicyDraft((current) => {
      if (current === null) {
        return current;
      }
      return { ...current, [field]: value };
    });
    setPolicyDirty(true);
  }

  function handlePolicyNumberChange(field: keyof HemsPolicyRead) {
    return (event: ChangeEvent<HTMLInputElement>) => {
      const nextValue = Number(event.target.value);
      updatePolicyField(field, Number.isFinite(nextValue) ? nextValue : 0);
    };
  }

  function handlePolicyTextChange(field: keyof HemsPolicyRead) {
    return (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
      updatePolicyField(field, event.target.value);
    };
  }

  function handlePolicyToggle(field: keyof HemsPolicyRead) {
    return (event: ChangeEvent<HTMLInputElement>) => {
      updatePolicyField(field, event.target.checked);
    };
  }

  async function handleReplan() {
    if (policyDraft === null) {
      return;
    }

    setBusyAction("run-replan");
    setError(null);
    setNotice(null);

    try {
      if (policyDirty) {
        const updatedPolicy = await api.updateHemsPolicy(buildPolicyPayload(policyDraft));
        setPolicyDraft(updatedPolicy);
        setPolicyDirty(false);
      }

      const nextPlan = await api.runHemsReplan();
      const [summary, assets] = await Promise.all([api.getHemsSummary(), api.listHemsAssets()]);
      setHemsPlan(nextPlan);
      setHemsSummary(summary);
      setHemsAssets(assets);
      setPolicyDraft(summary.policy);
      setNotice("HEMS plan refreshed from the current asset snapshot and policy.");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to replan HEMS.");
    } finally {
      setBusyAction(null);
    }
  }

  function toggleDevice(deviceId: string) {
    setSelectedDeviceId((current) => (current === deviceId ? null : deviceId));
  }

  function changeDevicePanelView(deviceId: string, view: DevicePanelView) {
    setDevicePanelViews((current) => ({ ...current, [deviceId]: view }));
  }

  function handleDeviceTouchStart(deviceId: string, event: TouchEvent<HTMLDivElement>) {
    touchStartX.current[deviceId] = event.touches[0]?.clientX ?? 0;
  }

  function handleDeviceTouchEnd(deviceId: string, event: TouchEvent<HTMLDivElement>) {
    const start = touchStartX.current[deviceId];
    const end = event.changedTouches[0]?.clientX ?? start;
    const delta = end - start;

    if (Math.abs(delta) < 40) {
      return;
    }

    changeDevicePanelView(deviceId, delta < 0 ? "monitoring" : "details");
  }

  function renderDeviceDetails(device: DeviceRead) {
    const panelView = devicePanelViews[device.id] ?? "details";
    const samples = telemetryHistory[device.id] ?? [];
    const metric = deviceMetricSelection[device.id] ?? null;

    return (
      <div className="device-inline-details">
        <div className="device-detail-shell">
          <div className="device-detail-tabs" role="tablist" aria-label={`${device.name} details`}>
            <button
              type="button"
              className={`device-detail-tab ${panelView === "details" ? "active" : ""}`}
              onClick={() => changeDevicePanelView(device.id, "details")}
            >
              Details
            </button>
            <button
              type="button"
              className={`device-detail-tab ${panelView === "monitoring" ? "active" : ""}`}
              onClick={() => changeDevicePanelView(device.id, "monitoring")}
            >
              Monitoring
            </button>
          </div>

          <div
            className="device-detail-carousel"
            onTouchStart={(event) => handleDeviceTouchStart(device.id, event)}
            onTouchEnd={(event) => handleDeviceTouchEnd(device.id, event)}
          >
            <div className={`device-detail-track ${panelView === "monitoring" ? "show-monitoring" : "show-details"}`}>
              <div className="device-detail-pane">
                <div className="panel-stack">
                  <div className="sub-block no-border">
                    <div className="tag-row">
                      {device.protocols.map((protocol) => (
                        <span key={`${device.id}-${protocol}`} className="soft-tag">
                          {protocol}
                        </span>
                      ))}
                    </div>
                    <p className="inline-note">{device.explanation}</p>
                  </div>

                  <div className="sub-block">
                    <h4>Capabilities</h4>
                    <div className="tag-row">
                      <CapabilityPill label="Visible" enabled={device.capabilities.visible} />
                      <CapabilityPill label="Monitorable" enabled={device.capabilities.monitorable} />
                      <CapabilityPill label="Controllable" enabled={device.capabilities.controllable} />
                      <CapabilityPill label="Optimizable" enabled={device.capabilities.optimizable} />
                    </div>
                  </div>

                  <div className="sub-block">
                    <h4>Live data</h4>
                    <dl className="data-grid">
                      {Object.entries(device.telemetry).map(([key, value]) => (
                        <div key={`${device.id}-${key}`} className="data-point">
                          <dt>{humanizeLabel(key)}</dt>
                          <dd>{formatValue(value)}</dd>
                        </div>
                      ))}
                    </dl>
                  </div>

                  <div className="sub-block">
                    <h4>Next step</h4>
                    <p className="inline-note">{device.next_step}</p>
                    <p className="inline-note">Last seen {formatDateTime(device.last_seen_at)}</p>
                  </div>
                </div>
              </div>

              <div className="device-detail-pane">
                <MonitoringPanel
                  device={device}
                  samples={samples}
                  selectedMetric={metric}
                  onSelectMetric={(metricKey) =>
                    setDeviceMetricSelection((current) => ({ ...current, [device.id]: metricKey }))
                  }
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="loading-screen">
        <h1>Helios Home</h1>
        <p>Loading the local device graph and HEMS runtime.</p>
      </div>
    );
  }

  const activeTitle = currentPage === "devices" ? "Devices" : "HEMS";
  const selectedScopeText = selectedSubnets.length > 0 ? selectedSubnets.join(", ") : "No subnet selected yet.";
  const latestPlanHeader = hemsPlan ?? hemsSummary?.latest_plan ?? null;
  const intervalGroups = planIntervalsByAsset(hemsPlan, hemsAssets);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true" />
          <h1>Helios Home</h1>
        </div>

        <nav className="nav-list" aria-label="Primary">
          <button
            type="button"
            className={`nav-link ${currentPage === "devices" ? "active" : ""}`}
            onClick={() => setCurrentPage("devices")}
          >
            Devices
          </button>
          <button
            type="button"
            className={`nav-link ${currentPage === "hems" ? "active" : ""}`}
            onClick={() => setCurrentPage("hems")}
          >
            HEMS
          </button>
        </nav>

        <div className="sidebar-footer">© 2026 NeurHelios</div>
      </aside>

      <main className="workspace">
        {error ? <div className="error-banner">{error}</div> : null}
        {notice ? <div className="notice-banner">{notice}</div> : null}

        <header className="masthead">
          <div>
            <h2>{activeTitle}</h2>
          </div>
          <div className="masthead-actions">
            {currentPage === "devices" ? (
              <>
                <button
                  type="button"
                  className="button-secondary"
                  onClick={() => void handleRefreshDevices()}
                  disabled={busyAction !== null}
                >
                  Refresh
                </button>
                <button
                  type="button"
                  className="button-primary"
                  onClick={() => void handleDiscoveryRun()}
                  disabled={busyAction !== null}
                >
                  Run discovery
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  className="button-secondary"
                  onClick={() => void handleRefreshHems()}
                  disabled={busyAction !== null}
                >
                  Refresh
                </button>
                <button
                  type="button"
                  className="button-primary"
                  onClick={() => void handleReplan()}
                  disabled={busyAction !== null || policyDraft === null}
                >
                  Run replan
                </button>
              </>
            )}
          </div>
        </header>

        {currentPage === "devices" ? (
          <div className="page-layout home-layout">
            <section className="section-panel span-12">
              <div className="section-title">
                <h3>Network selection</h3>
              </div>
              <div className="note-lines network-hint">
                <p>Select the reachable subnets Helios should scan. The current selection is used directly when you run discovery.</p>
              </div>
              {reachableSubnets.length > 0 ? (
                <div className="checkbox-list">
                  {reachableSubnets.map((subnet) => {
                    const selected = selectedSubnets.includes(subnet.cidr);
                    return (
                      <label
                        key={subnet.cidr}
                        className={`checkbox-row ${selected ? "selected" : ""}`}
                        title={`Reachable via ${subnet.interface}`}
                      >
                        <input
                          type="checkbox"
                          checked={selected}
                          onChange={() => toggleSubnet(subnet.cidr)}
                        />
                        <span>{subnet.cidr}</span>
                        <small>{subnet.interface}</small>
                      </label>
                    );
                  })}
                </div>
              ) : (
                <div className="empty-state">
                  <h4>No reachable subnets detected</h4>
                  <p>Helios could not derive local IPv4 routes from this host.</p>
                </div>
              )}
              <div className="form-foot">
                <span className="inline-note">{selectedScopeText}</span>
              </div>
            </section>

            <section className="section-panel span-12">
              <div className="section-title">
                <h3>Found devices</h3>
              </div>
              {overview && overview.devices.length > 0 ? (
                <div className="device-list">
                  {overview.devices.map((device) => {
                    const expanded = selectedDeviceId === device.id;
                    return (
                      <article
                        key={device.id}
                        className={`device-list-item ${expanded ? "expanded" : ""}`}
                        title={`${device.manufacturer} ${device.model}`}
                      >
                        <button
                          type="button"
                          className="device-row-button"
                          onClick={() => toggleDevice(device.id)}
                        >
                          <div className="row-main">
                            <strong>{device.name}</strong>
                            <span>
                              {humanizeLabel(device.device_type)} · {device.manufacturer || "Unknown maker"} ·{" "}
                              {device.model || "Unknown model"}
                            </span>
                          </div>
                          <div className="row-side">
                            {device.protocols.map((protocol) => (
                              <span key={`${device.id}-${protocol}`} className="soft-tag">
                                {protocol}
                              </span>
                            ))}
                            <StatusBadge status={device.primary_status} />
                          </div>
                        </button>
                        {expanded ? renderDeviceDetails(device) : null}
                      </article>
                    );
                  })}
                </div>
              ) : (
                <div className="empty-state">
                  <h4>No devices discovered yet</h4>
                  <p>Choose a subnet above and run discovery to build the local inventory.</p>
                </div>
              )}
            </section>
          </div>
        ) : (
          <div className="page-layout">
            <section className="section-panel span-12">
              <div className="section-title">
                <h3>HEMS status</h3>
              </div>
              {hemsSummary ? (
                <div className="site-strip hems-strip">
                  <div className="site-stat">
                    <span>Assets</span>
                    <strong>{hemsSummary.asset_count}</strong>
                    <small>{hemsSummary.dispatchable_asset_count} dispatchable</small>
                  </div>
                  <div className="site-stat">
                    <span>Execution</span>
                    <strong>{humanizeLabel(hemsSummary.policy.execution_mode)}</strong>
                    <small>{hemsSummary.plan_only_asset_count} plan only</small>
                  </div>
                  <div className="site-stat">
                    <span>Latest plan</span>
                    <strong>{latestPlanHeader ? humanizeLabel(latestPlanHeader.status) : "Not run yet"}</strong>
                    <small>{latestPlanHeader ? formatDateTime(latestPlanHeader.created_at) : "No plan recorded"}</small>
                  </div>
                  <div className="site-stat">
                    <span>Guardrails</span>
                    <strong>{hemsSummary.blocked_asset_count} blocked</strong>
                    <small>{hemsSummary.read_only_asset_count} read only</small>
                  </div>
                </div>
              ) : (
                <div className="empty-state">
                  <h4>No HEMS summary available</h4>
                  <p>The HEMS service has not returned a site model yet.</p>
                </div>
              )}
            </section>

            <section className="section-panel span-12">
              <div className="section-title">
                <h3>Policy</h3>
              </div>
              {policyDraft ? (
                <>
                  <div className="note-lines">
                    <p>Edit policy values here and apply them with the next replan.</p>
                  </div>
                  <div className="policy-grid">
                    <label>
                      <span>Execution mode</span>
                      <select value={policyDraft.execution_mode} onChange={handlePolicyTextChange("execution_mode")}>
                        <option value="guarded_auto">Guarded auto</option>
                        <option value="plan_only">Plan only</option>
                      </select>
                    </label>
                    <label>
                      <span>Battery reserve (%)</span>
                      <input
                        type="number"
                        step="1"
                        value={policyDraft.battery_reserve_pct}
                        onChange={handlePolicyNumberChange("battery_reserve_pct")}
                      />
                    </label>
                    <label>
                      <span>EV target SOC (%)</span>
                      <input
                        type="number"
                        step="1"
                        value={policyDraft.ev_default_target_soc_pct}
                        onChange={handlePolicyNumberChange("ev_default_target_soc_pct")}
                      />
                    </label>
                    <label>
                      <span>Default departure</span>
                      <input
                        type="time"
                        value={policyDraft.ev_default_departure_time}
                        onChange={handlePolicyTextChange("ev_default_departure_time")}
                      />
                    </label>
                    <label>
                      <span>Heat comfort min (°C)</span>
                      <input
                        type="number"
                        step="0.5"
                        value={policyDraft.heat_comfort_min_c}
                        onChange={handlePolicyNumberChange("heat_comfort_min_c")}
                      />
                    </label>
                    <label>
                      <span>Heat comfort max (°C)</span>
                      <input
                        type="number"
                        step="0.5"
                        value={policyDraft.heat_comfort_max_c}
                        onChange={handlePolicyNumberChange("heat_comfort_max_c")}
                      />
                    </label>
                    <label>
                      <span>Grid import limit (kW)</span>
                      <input
                        type="number"
                        step="0.5"
                        value={policyDraft.grid_import_limit_kw}
                        onChange={handlePolicyNumberChange("grid_import_limit_kw")}
                      />
                    </label>
                    <label>
                      <span>Grid export limit (kW)</span>
                      <input
                        type="number"
                        step="0.5"
                        value={policyDraft.grid_export_limit_kw}
                        onChange={handlePolicyNumberChange("grid_export_limit_kw")}
                      />
                    </label>
                    <label>
                      <span>Horizon (hours)</span>
                      <input
                        type="number"
                        step="1"
                        value={policyDraft.horizon_hours}
                        onChange={handlePolicyNumberChange("horizon_hours")}
                      />
                    </label>
                    <label>
                      <span>Step size (minutes)</span>
                      <input
                        type="number"
                        step="5"
                        value={policyDraft.step_minutes}
                        onChange={handlePolicyNumberChange("step_minutes")}
                      />
                    </label>
                  </div>

                  <div className="toggle-grid">
                    <label className={`checkbox-row ${policyDraft.allow_price_arbitrage ? "selected" : ""}`}>
                      <input
                        type="checkbox"
                        checked={policyDraft.allow_price_arbitrage}
                        onChange={handlePolicyToggle("allow_price_arbitrage")}
                      />
                      <span>Allow price arbitrage</span>
                      <small>policy</small>
                    </label>
                    <label className={`checkbox-row ${policyDraft.allow_heat_precharge ? "selected" : ""}`}>
                      <input
                        type="checkbox"
                        checked={policyDraft.allow_heat_precharge}
                        onChange={handlePolicyToggle("allow_heat_precharge")}
                      />
                      <span>Allow heat precharge</span>
                      <small>policy</small>
                    </label>
                    <label className={`checkbox-row ${policyDraft.allow_ev_load_shifting ? "selected" : ""}`}>
                      <input
                        type="checkbox"
                        checked={policyDraft.allow_ev_load_shifting}
                        onChange={handlePolicyToggle("allow_ev_load_shifting")}
                      />
                      <span>Allow EV load shifting</span>
                      <small>policy</small>
                    </label>
                  </div>
                </>
              ) : (
                <div className="empty-state">
                  <h4>No HEMS policy available</h4>
                  <p>The planner policy has not been initialized yet.</p>
                </div>
              )}
            </section>

            <section className="section-panel span-12">
              <div className="section-title">
                <h3>Canonical assets</h3>
              </div>
              {hemsAssets.length > 0 ? (
                <ul className="line-list">
                  {hemsAssets.map((asset) => (
                    <li key={asset.asset_key} className="line-row hems-asset-row">
                      <div className="row-main">
                        <strong>{asset.label}</strong>
                        <span>
                          {humanizeLabel(asset.asset_type)} · {humanizeLabel(asset.control_capability)}
                        </span>
                        {asset.reasons.length > 0 ? (
                          <div className="tag-row">
                            {asset.reasons.map((reason) => (
                              <span key={`${asset.asset_key}-${reason}`} className="soft-tag">
                                {humanize(reason)}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        <div className="detail-pairs">
                          <div>
                            <small>Telemetry</small>
                            <p>
                              {summarizeEntries(asset.telemetry)
                                .map(([key, value]) => `${humanize(key)} ${formatValue(value)}`)
                                .join(" • ") || "No live telemetry mapped"}
                            </p>
                          </div>
                          <div>
                            <small>Constraints</small>
                            <p>
                              {summarizeEntries(asset.constraints)
                                .map(([key, value]) => `${humanize(key)} ${formatValue(value)}`)
                                .join(" • ") || "No explicit constraints available"}
                            </p>
                          </div>
                        </div>
                      </div>
                      <div className="row-side">
                        <span className={`status-badge ${toneForEligibility(asset.eligibility)}`}>
                          {humanize(asset.eligibility)}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="empty-state">
                  <h4>No HEMS assets available</h4>
                  <p>Run discovery first so the HEMS builder can map canonical assets.</p>
                </div>
              )}
            </section>

            <section className="section-panel span-12">
              <div className="section-title">
                <h3>Latest plan</h3>
              </div>
              {latestPlanHeader ? (
                <div className="panel-stack">
                  <div className="list-header">
                    <div className="row-main">
                      <strong>{humanizeLabel(latestPlanHeader.status)}</strong>
                      <span>
                        {humanizeLabel(latestPlanHeader.execution_mode)} · {latestPlanHeader.summary}
                      </span>
                    </div>
                    <div className="row-side">
                      <span className="soft-tag">{latestPlanHeader.solver_name}</span>
                      <span className="soft-tag">{formatDateTime(latestPlanHeader.created_at)}</span>
                    </div>
                  </div>

                  {hemsPlan ? (
                    <>
                      <div className="plan-grid">
                        {intervalGroups.length > 0 ? (
                          intervalGroups.map((group) => (
                            <article key={group.assetKey} className="plan-group">
                              <div className="list-header">
                                <div className="row-main">
                                  <strong>{group.asset?.label ?? group.assetKey}</strong>
                                  <span>{humanizeLabel(group.asset?.asset_type ?? "asset")}</span>
                                </div>
                                {group.asset ? (
                                  <span className={`status-badge ${toneForEligibility(group.asset.eligibility)}`}>
                                    {humanize(group.asset.eligibility)}
                                  </span>
                                ) : null}
                              </div>
                              <div className="plan-interval-list">
                                {group.intervals.slice(0, 5).map((interval) => (
                                  <div key={`${interval.asset_key}-${interval.starts_at}`} className="plan-interval">
                                    <div className="row-main">
                                      <strong>{formatTimeRange(interval.starts_at, interval.ends_at)}</strong>
                                      <span>{commandSummary(interval.command)}</span>
                                    </div>
                                    {Object.keys(interval.predicted_state).length > 0 ? (
                                      <div className="tag-row">
                                        {summarizeEntries(interval.predicted_state, 2).map(([key, value]) => (
                                          <span key={`${interval.asset_key}-${interval.starts_at}-${key}`} className="soft-tag">
                                            {humanize(key)} {formatValue(value)}
                                          </span>
                                        ))}
                                      </div>
                                    ) : null}
                                  </div>
                                ))}
                                {group.intervals.length > 5 ? (
                                  <p className="inline-note">+ {group.intervals.length - 5} more planned intervals</p>
                                ) : null}
                              </div>
                            </article>
                          ))
                        ) : (
                          <div className="empty-state">
                            <h4>No interval schedule stored</h4>
                            <p>The latest plan did not persist interval-level commands.</p>
                          </div>
                        )}
                      </div>

                      <div className="split-grid">
                        <div className="sub-block no-border">
                          <h4>Dispatch</h4>
                          {hemsPlan.dispatch_events.length > 0 ? (
                            <ul className="line-list">
                              {hemsPlan.dispatch_events.slice(0, 8).map((event: HemsDispatchEventRead) => (
                                <li key={event.id} className="line-row slim">
                                  <div className="row-main">
                                    <strong>{event.summary}</strong>
                                    <span>
                                      {formatDateTime(event.executed_at)} · {commandSummary(event.applied_command || event.requested_command)}
                                    </span>
                                  </div>
                                  <div className="row-side">
                                    <span className={`status-badge ${toneForDispatchStatus(event.status)}`}>
                                      {humanize(event.status)}
                                    </span>
                                  </div>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="inline-note">No dispatch events were recorded for this plan.</p>
                          )}
                        </div>

                        <div className="sub-block no-border">
                          <h4>Violations</h4>
                          {hemsPlan.violations.length > 0 ? (
                            <ul className="line-list">
                              {hemsPlan.violations.slice(0, 8).map((violation) => (
                                <li key={violation.id} className="line-row slim">
                                  <div className="row-main">
                                    <strong>{violation.message}</strong>
                                    <span>
                                      {humanizeLabel(violation.violation_type)} · {formatDateTime(violation.created_at)}
                                    </span>
                                  </div>
                                  <div className="row-side">
                                    <span className={`status-badge ${toneForViolationSeverity(violation.severity)}`}>
                                      {humanize(violation.severity)}
                                    </span>
                                  </div>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="inline-note">No violations were recorded for this plan.</p>
                          )}
                        </div>
                      </div>
                    </>
                  ) : (
                    <div className="empty-state">
                      <h4>Plan header only</h4>
                      <p>The latest plan metadata exists, but detailed intervals are not loaded yet.</p>
                    </div>
                  )}
                </div>
              ) : (
                <div className="empty-state">
                  <h4>No HEMS plan yet</h4>
                  <p>Run the first HEMS replan to create a dispatch snapshot for this site.</p>
                </div>
              )}
            </section>
          </div>
        )}
      </main>
    </div>
  );
}
