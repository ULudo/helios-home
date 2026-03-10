import { Dispatch, SetStateAction, TouchEvent, startTransition, useEffect, useRef, useState } from "react";

import { CapabilityPill } from "./components/CapabilityPill";
import { StatusBadge } from "./components/StatusBadge";
import { api } from "./lib/api";
import type { DeviceRead, OverviewResponse, ReachableSubnetRead } from "./lib/types";

type DevicePanelView = "details" | "monitoring";

type TelemetrySnapshot = {
  recorded_at: string;
  telemetry: Record<string, string | number | boolean>;
};

type TelemetryHistory = Record<string, TelemetrySnapshot[]>;

const MONITORING_HISTORY_LIMIT = 48;
const MONITORING_METRIC_PRIORITY = [
  "power_kw",
  "power_w",
  "grid_power_kw",
  "soc_pct",
  "energy_total_kwh",
  "energy_today_kwh",
  "voltage_v",
  "current_a",
  "temperature_c",
];

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function humanize(value: string): string {
  return value.split("_").join(" ");
}

function formatMetricLabel(metricKey: string): string {
  return humanize(metricKey).replace(/\b\w/g, (character) => character.toUpperCase());
}

function parseSubnetConfig(value: string): string[] {
  return value
    .split(/[,\n;]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function normalizedSubnetSelection(subnets: string[]): string[] {
  return [...subnets].sort((left, right) => left.localeCompare(right));
}

function mergeSubnetOptions(selectedSubnets: string[], discoveredSubnets: ReachableSubnetRead[]): ReachableSubnetRead[] {
  const merged = new Map<string, ReachableSubnetRead>();
  for (const subnet of discoveredSubnets) {
    merged.set(subnet.cidr, subnet);
  }
  for (const subnet of selectedSubnets) {
    if (!merged.has(subnet)) {
      merged.set(subnet, {
        cidr: subnet,
        interface: "saved",
        label: `${subnet} (saved)`,
      });
    }
  }
  return Array.from(merged.values()).sort((left, right) => left.label.localeCompare(right.label));
}

function toNumericValue(value: string | number | boolean | undefined): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  if (typeof value !== "string") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function appendTelemetryHistory(currentHistory: TelemetryHistory, devices: DeviceRead[]): TelemetryHistory {
  const nextHistory: TelemetryHistory = { ...currentHistory };
  const recordedAt = new Date().toISOString();

  for (const device of devices) {
    if (!Object.keys(device.telemetry).length) {
      continue;
    }
    const deviceHistory = nextHistory[device.id] ?? [];
    nextHistory[device.id] = [
      ...deviceHistory,
      {
        recorded_at: recordedAt,
        telemetry: device.telemetry,
      },
    ].slice(-MONITORING_HISTORY_LIMIT);
  }

  return nextHistory;
}

function metricSortScore(metricKey: string): number {
  const priorityIndex = MONITORING_METRIC_PRIORITY.indexOf(metricKey);
  return priorityIndex === -1 ? MONITORING_METRIC_PRIORITY.length + 1 : priorityIndex;
}

function listNumericTelemetryMetrics(device: DeviceRead, history: TelemetrySnapshot[]): string[] {
  const metricKeys = new Set<string>();

  for (const [key, value] of Object.entries(device.telemetry)) {
    if (toNumericValue(value) !== null) {
      metricKeys.add(key);
    }
  }

  for (const snapshot of history) {
    for (const [key, value] of Object.entries(snapshot.telemetry)) {
      if (toNumericValue(value) !== null) {
        metricKeys.add(key);
      }
    }
  }

  return Array.from(metricKeys).sort((left, right) => {
    const scoreDifference = metricSortScore(left) - metricSortScore(right);
    return scoreDifference !== 0 ? scoreDifference : left.localeCompare(right);
  });
}

function buildMetricSeries(history: TelemetrySnapshot[], metricKey: string): Array<{ recorded_at: string; value: number }> {
  return history
    .map((snapshot) => ({
      recorded_at: snapshot.recorded_at,
      value: toNumericValue(snapshot.telemetry[metricKey]) ?? NaN,
    }))
    .filter((point) => Number.isFinite(point.value));
}

function App() {
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [reachableSubnets, setReachableSubnets] = useState<ReachableSubnetRead[]>([]);
  const [telemetryHistory, setTelemetryHistory] = useState<TelemetryHistory>({});
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);
  const [devicePanelView, setDevicePanelView] = useState<DevicePanelView>("details");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formState, setFormState] = useState({
    local_subnets: [] as string[],
  });

  async function loadOverview(options?: { silent?: boolean }) {
    if (!options?.silent) {
      setLoading(true);
    }

    try {
      const data = await api.getOverview();
      startTransition(() => {
        setOverview(data);
        setTelemetryHistory((current) => appendTelemetryHistory(current, data.devices));
        setError(null);
      });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unexpected error");
    } finally {
      if (!options?.silent) {
        setLoading(false);
      }
    }
  }

  async function loadReachableSubnets(options?: { silent?: boolean }) {
    try {
      const subnets = await api.listReachableSubnets();
      startTransition(() => {
        setReachableSubnets(subnets);
      });
    } catch (requestError) {
      if (!options?.silent) {
        setError(requestError instanceof Error ? requestError.message : "Unable to load reachable subnets");
      }
    }
  }

  async function loadInitialData() {
    setLoading(true);
    try {
      const [overviewData, subnets] = await Promise.all([api.getOverview(), api.listReachableSubnets()]);
      startTransition(() => {
        setOverview(overviewData);
        setReachableSubnets(subnets);
        setTelemetryHistory((current) => appendTelemetryHistory(current, overviewData.devices));
        setError(null);
      });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to load application state");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadInitialData();
  }, []);

  useEffect(() => {
    if (!overview) {
      return;
    }

    setFormState({
      local_subnets: parseSubnetConfig(overview.site.local_subnet),
    });

    if (selectedDeviceId && !overview.devices.some((device) => device.id === selectedDeviceId)) {
      setSelectedDeviceId(null);
      setDevicePanelView("details");
    }
  }, [overview, selectedDeviceId]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      if (document.visibilityState === "hidden") {
        return;
      }
      void loadOverview({ silent: true });
    }, 15000);

    return () => window.clearInterval(intervalId);
  }, []);

  async function handleDiscovery() {
    setBusyAction("discovery");
    setError(null);
    try {
      const selectedSubnets = normalizedSubnetSelection(formState.local_subnets);
      const savedSubnets = normalizedSubnetSelection(parseSubnetConfig(overview?.site.local_subnet ?? ""));
      if (selectedSubnets.join("|") !== savedSubnets.join("|")) {
        await api.updateSite({
          local_subnet: formState.local_subnets.join(", "),
        });
      }
      await api.runDiscovery();
      await loadOverview({ silent: true });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Discovery failed");
    } finally {
      setBusyAction(null);
    }
  }

  if (loading && !overview) {
    return (
      <div className="loading-screen">
        <p className="eyebrow">Helios Home</p>
        <h1>Loading workspace</h1>
        <p>Fetching discovery inventory and monitoring data.</p>
      </div>
    );
  }

  if (!overview) {
    return (
      <div className="loading-screen">
        <p className="eyebrow">Helios Home</p>
        <h1>Unable to load the application</h1>
        <p>{error ?? "The backend did not return a usable overview payload."}</p>
        <button className="button-primary" onClick={() => void loadInitialData()} type="button">
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <span className="brand-mark" />
          <h1>Helios Home</h1>
        </div>

        <footer className="sidebar-footer">© 2026 NeurHelios</footer>
      </aside>

      <main className="workspace">
        <header className="masthead">
          <h2>Devices</h2>
          <div className="masthead-actions">
            <button
              className="button-secondary"
              onClick={() => void Promise.all([loadOverview({ silent: true }), loadReachableSubnets({ silent: true })])}
              type="button"
            >
              Refresh
            </button>
            <button
              className="button-primary"
              disabled={busyAction === "discovery"}
              onClick={() => void handleDiscovery()}
              type="button"
            >
              {busyAction === "discovery" ? "Running..." : "Run discovery"}
            </button>
          </div>
        </header>

        {error ? <div className="error-banner">{error}</div> : null}

        <DevicesPage
          devicePanelView={devicePanelView}
          formState={formState}
          overview={overview}
          reachableSubnets={reachableSubnets}
          selectedDeviceId={selectedDeviceId}
          setDevicePanelView={setDevicePanelView}
          setFormState={setFormState}
          setSelectedDeviceId={setSelectedDeviceId}
          telemetryHistory={telemetryHistory}
        />
      </main>
    </div>
  );
}

function DevicesPage({
  overview,
  reachableSubnets,
  formState,
  setFormState,
  devicePanelView,
  selectedDeviceId,
  setDevicePanelView,
  setSelectedDeviceId,
  telemetryHistory,
}: {
  overview: OverviewResponse;
  reachableSubnets: ReachableSubnetRead[];
  formState: {
    local_subnets: string[];
  };
  setFormState: Dispatch<
    SetStateAction<{
      local_subnets: string[];
    }>
  >;
  devicePanelView: DevicePanelView;
  selectedDeviceId: string | null;
  setDevicePanelView: Dispatch<SetStateAction<DevicePanelView>>;
  setSelectedDeviceId: Dispatch<SetStateAction<string | null>>;
  telemetryHistory: TelemetryHistory;
}) {
  const subnetOptions = mergeSubnetOptions(formState.local_subnets, reachableSubnets);

  return (
    <div className="page-layout home-layout">
      <section className="section-panel span-12">
        <SectionTitle title="Reachable networks" hint="Select the network segments Helios should scan." />
        <div className="input-grid">
          <p className="inline-note network-hint">Choose the networks to scan. The current selection is used when you run discovery.</p>
          <div className="checkbox-list" role="group" aria-label="Reachable subnets">
            {subnetOptions.length ? (
              subnetOptions.map((option) => {
                const selected = formState.local_subnets.includes(option.cidr);
                return (
                  <label className={`checkbox-row ${selected ? "selected" : ""}`} key={option.cidr} title={option.label}>
                    <input
                      checked={selected}
                      onChange={() =>
                        setFormState((current) => ({
                          ...current,
                          local_subnets: selected
                            ? current.local_subnets.filter((subnet) => subnet !== option.cidr)
                            : [...current.local_subnets, option.cidr],
                        }))
                      }
                      type="checkbox"
                    />
                    <span>{option.cidr}</span>
                    <small>{option.interface}</small>
                  </label>
                );
              })
            ) : (
              <p className="inline-note">No reachable IPv4 subnet was detected on this host yet.</p>
            )}
          </div>
        </div>
      </section>

      <section className="section-panel span-12">
        <SectionTitle title="Inventory" hint="Discovered devices with inline details and monitoring." />
        {overview.devices.length ? (
          <ul className="line-list device-list">
            {overview.devices.map((device) => (
              <li className={`device-list-item ${selectedDeviceId === device.id ? "expanded" : ""}`} key={device.id}>
                <button
                  aria-expanded={selectedDeviceId === device.id}
                  className="device-row-button"
                  onClick={() => {
                    setSelectedDeviceId((current) => {
                      const nextDeviceId = current === device.id ? null : device.id;
                      setDevicePanelView("details");
                      return nextDeviceId;
                    });
                  }}
                  type="button"
                >
                  <div className="row-main">
                    <strong>{device.name}</strong>
                    <span>
                      {device.manufacturer} {device.model}
                    </span>
                  </div>
                  <div className="row-side">
                    <div className="tag-row">
                      {device.protocols.map((protocol) => (
                        <span className="soft-tag" key={protocol}>
                          {humanize(protocol)}
                        </span>
                      ))}
                    </div>
                    <StatusBadge status={device.primary_status} />
                    <span className="soft-tag">{formatDateTime(device.last_seen_at)}</span>
                  </div>
                </button>

                {selectedDeviceId === device.id ? (
                  <div className="device-inline-details">
                    <DeviceDetailsContent
                      device={device}
                      history={telemetryHistory[device.id] ?? []}
                      onViewChange={setDevicePanelView}
                      view={devicePanelView}
                    />
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState title="No devices yet" description="Run discovery after selecting at least one reachable network." />
        )}
      </section>
    </div>
  );
}

function DeviceDetailsContent({
  device,
  history,
  onViewChange,
  view,
}: {
  device: DeviceRead;
  history: TelemetrySnapshot[];
  onViewChange: Dispatch<SetStateAction<DevicePanelView>>;
  view: DevicePanelView;
}) {
  const touchStartX = useRef<number | null>(null);
  const touchStartY = useRef<number | null>(null);

  function handleTouchStart(event: TouchEvent<HTMLDivElement>) {
    const touch = event.changedTouches[0];
    touchStartX.current = touch.clientX;
    touchStartY.current = touch.clientY;
  }

  function handleTouchEnd(event: TouchEvent<HTMLDivElement>) {
    if (touchStartX.current === null || touchStartY.current === null) {
      return;
    }

    const touch = event.changedTouches[0];
    const deltaX = touch.clientX - touchStartX.current;
    const deltaY = touch.clientY - touchStartY.current;
    touchStartX.current = null;
    touchStartY.current = null;

    if (Math.abs(deltaX) < 40 || Math.abs(deltaX) < Math.abs(deltaY)) {
      return;
    }

    if (view === "details" && deltaX > 0) {
      onViewChange("monitoring");
    } else if (view === "monitoring" && deltaX < 0) {
      onViewChange("details");
    }
  }

  return (
    <div className="device-detail-shell">
      <div className="device-detail-tabs" role="tablist" aria-label={`${device.name} detail views`}>
        <button
          aria-selected={view === "details"}
          className={`device-detail-tab ${view === "details" ? "active" : ""}`}
          onClick={() => onViewChange("details")}
          role="tab"
          type="button"
        >
          Details
        </button>
        <button
          aria-selected={view === "monitoring"}
          className={`device-detail-tab ${view === "monitoring" ? "active" : ""}`}
          onClick={() => onViewChange("monitoring")}
          role="tab"
          type="button"
        >
          Monitoring
        </button>
      </div>

      <div className="device-detail-carousel" onTouchEnd={handleTouchEnd} onTouchStart={handleTouchStart}>
        <div className={`device-detail-track ${view === "monitoring" ? "show-monitoring" : "show-details"}`}>
          <section aria-hidden={view !== "details"} className="device-detail-pane" role="tabpanel">
            <div className="panel-stack">
              <dl className="data-grid">
                <DataPoint label="Manufacturer" value={device.manufacturer} />
                <DataPoint label="Model" value={device.model} />
                <DataPoint label="Firmware" value={device.firmware} />
                <DataPoint label="Type" value={humanize(device.device_type)} />
              </dl>

              <div className="sub-block">
                <h4>Capabilities</h4>
                <div className="tag-row">
                  <CapabilityPill label="visible" enabled={device.capabilities.visible} />
                  <CapabilityPill label="monitorable" enabled={device.capabilities.monitorable} />
                  <CapabilityPill label="controllable" enabled={device.capabilities.controllable} />
                  <CapabilityPill label="optimizable" enabled={device.capabilities.optimizable} />
                </div>
              </div>

              <div className="sub-block">
                <h4>Telemetry</h4>
                {Object.keys(device.telemetry).length ? (
                  <dl className="data-grid">
                    {Object.entries(device.telemetry).map(([key, value]) => (
                      <DataPoint key={key} label={humanize(key)} value={String(value)} />
                    ))}
                  </dl>
                ) : (
                  <p className="inline-note">No validated telemetry is attached to this device yet.</p>
                )}
              </div>

              <div className="sub-block">
                <h4>Explanation</h4>
                <p>{device.explanation}</p>
                <small>{device.next_step}</small>
              </div>
            </div>
          </section>

          <section aria-hidden={view !== "monitoring"} className="device-detail-pane" role="tabpanel">
            <MonitoringPanel device={device} history={history} />
          </section>
        </div>
      </div>
    </div>
  );
}

function MonitoringPanel({ device, history }: { device: DeviceRead; history: TelemetrySnapshot[] }) {
  const metrics = listNumericTelemetryMetrics(device, history);
  const [selectedMetric, setSelectedMetric] = useState<string>(metrics[0] ?? "");

  useEffect(() => {
    if (!metrics.length) {
      setSelectedMetric("");
      return;
    }
    if (!selectedMetric || !metrics.includes(selectedMetric)) {
      setSelectedMetric(metrics[0]);
    }
  }, [metrics, selectedMetric]);

  const series = selectedMetric ? buildMetricSeries(history, selectedMetric) : [];

  return (
    <div className="panel-stack">
      <div className="list-header">
        <div>
          <strong>Session monitoring</strong>
          <p className="inline-note">History builds up from discovery refreshes and background polling in this browser session.</p>
        </div>
        <span className="soft-tag">{history.length} samples</span>
      </div>

      {metrics.length ? (
        <>
          <div className="tag-row monitoring-metric-picker">
            {metrics.map((metric) => (
              <button
                className={`metric-chip ${selectedMetric === metric ? "active" : ""}`}
                key={metric}
                onClick={() => setSelectedMetric(metric)}
                type="button"
              >
                {formatMetricLabel(metric)}
              </button>
            ))}
          </div>

          <MonitoringChart metricKey={selectedMetric} series={series} />
        </>
      ) : (
        <EmptyState
          title="No numeric telemetry yet"
          description="A monitoring chart becomes available once the device exposes numeric telemetry and the session has collected samples."
        />
      )}
    </div>
  );
}

function MonitoringChart({
  metricKey,
  series,
}: {
  metricKey: string;
  series: Array<{ recorded_at: string; value: number }>;
}) {
  if (!series.length) {
    return (
      <EmptyState
        title="No monitoring samples yet"
        description="Leave the app open for a short while or refresh discovery again to collect the first history points."
      />
    );
  }

  const values = series.map((point) => point.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const valueRange = maxValue - minValue || 1;
  const chartWidth = 720;
  const chartHeight = 210;
  const linePoints = series.map((point, index) => {
    const x = series.length === 1 ? chartWidth / 2 : (index / (series.length - 1)) * chartWidth;
    const y = chartHeight - ((point.value - minValue) / valueRange) * chartHeight;
    return `${x},${y}`;
  });
  const polylinePoints = linePoints.join(" ");
  const areaPoints = [`0,${chartHeight}`, ...linePoints, `${chartWidth},${chartHeight}`].join(" ");
  const lastPoint = series[series.length - 1];

  return (
    <div className="monitoring-panel">
      <div className="monitoring-header">
        <div>
          <p className="eyebrow">Monitoring</p>
          <h4>{formatMetricLabel(metricKey)}</h4>
        </div>
        <div className="monitoring-stats">
          <span className="soft-tag">Now {lastPoint.value}</span>
          <span className="soft-tag">Min {minValue}</span>
          <span className="soft-tag">Max {maxValue}</span>
        </div>
      </div>

      <svg className="monitoring-chart" viewBox={`0 0 ${chartWidth} ${chartHeight}`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="monitoring-area" x1="0%" x2="0%" y1="0%" y2="100%">
            <stop offset="0%" stopColor="rgba(45, 120, 96, 0.36)" />
            <stop offset="100%" stopColor="rgba(45, 120, 96, 0.02)" />
          </linearGradient>
        </defs>
        <polyline className="monitoring-area" fill="url(#monitoring-area)" points={areaPoints} stroke="none" />
        <polyline className="monitoring-line" fill="none" points={polylinePoints} />
        {linePoints.map((point, index) => {
          const [cx, cy] = point.split(",");
          return <circle className="monitoring-dot" cx={cx} cy={cy} key={`${metricKey}-${index}`} r={series.length < 3 ? 5 : 3.2} />;
        })}
      </svg>

      <div className="monitoring-footer">
        <span>{formatDateTime(series[0].recorded_at)}</span>
        <span>{formatDateTime(lastPoint.recorded_at)}</span>
      </div>
    </div>
  );
}

function DataPoint({ label, value }: { label: string; value: string }) {
  return (
    <div className="data-point" title={label}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function SectionTitle({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="section-title">
      <h3 title={hint}>{title}</h3>
    </div>
  );
}

function EmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty-state">
      <h4>{title}</h4>
      <p>{description}</p>
    </div>
  );
}

export default App;
