export interface SiteRead {
  id: number;
  local_subnet: string;
  updated_at: string;
}

export interface SiteUpdate {
  local_subnet?: string;
}

export interface ReachableSubnetRead {
  cidr: string;
  interface: string;
  label: string;
}

export interface CapabilityRead {
  visible: boolean;
  monitorable: boolean;
  controllable: boolean;
  optimizable: boolean;
}

export interface DeviceRead {
  id: string;
  name: string;
  manufacturer: string;
  model: string;
  firmware: string;
  device_type: string;
  primary_status: string;
  protocols: string[];
  capabilities: CapabilityRead;
  telemetry: Record<string, string | number | boolean>;
  explanation: string;
  next_step: string;
  last_seen_at: string;
}

export interface DiscoverySourceResultRead {
  source_name: string;
  status: string;
  message: string;
  candidate_count: number;
}

export interface DiscoveryRunRead {
  id?: string;
  status?: string;
  source_names: string[];
  source_results: DiscoverySourceResultRead[];
  executed_at: string;
  message: string;
  new_device_ids: string[];
  refreshed_devices: number;
  candidate_count: number;
  integrated_devices: number;
}

export interface OverviewResponse {
  site: SiteRead;
  devices: DeviceRead[];
}

export interface HemsPolicyRead {
  site_id: number;
  execution_mode: string;
  battery_reserve_pct: number;
  ev_default_target_soc_pct: number;
  ev_default_departure_time: string;
  heat_comfort_min_c: number;
  heat_comfort_max_c: number;
  grid_import_limit_kw: number;
  grid_export_limit_kw: number;
  allow_price_arbitrage: boolean;
  allow_heat_precharge: boolean;
  allow_ev_load_shifting: boolean;
  horizon_hours: number;
  step_minutes: number;
  updated_at: string;
}

export interface HemsPolicyUpdate {
  execution_mode?: string;
  battery_reserve_pct?: number;
  ev_default_target_soc_pct?: number;
  ev_default_departure_time?: string;
  heat_comfort_min_c?: number;
  heat_comfort_max_c?: number;
  grid_import_limit_kw?: number;
  grid_export_limit_kw?: number;
  allow_price_arbitrage?: boolean;
  allow_heat_precharge?: boolean;
  allow_ev_load_shifting?: boolean;
  horizon_hours?: number;
  step_minutes?: number;
}

export interface HemsAssetRead {
  asset_key: string;
  asset_type: string;
  label: string;
  device_id: string | null;
  control_capability: string;
  eligibility: string;
  telemetry: Record<string, string | number | boolean>;
  constraints: Record<string, string | number | boolean>;
  reasons: string[];
}

export interface HemsPlanHeaderRead {
  id: string;
  status: string;
  execution_mode: string;
  triggered_by: string;
  solver_name: string;
  objective_value: number | null;
  summary: string;
  horizon_start: string;
  horizon_end: string;
  created_at: string;
  finished_at: string | null;
}

export interface HemsPlanIntervalRead {
  id: number | null;
  asset_key: string;
  asset_type: string;
  device_id: string | null;
  starts_at: string;
  ends_at: string;
  command: Record<string, string | number | boolean>;
  predicted_state: Record<string, string | number | boolean>;
}

export interface HemsDispatchEventRead {
  id: number;
  asset_key: string;
  asset_type: string;
  device_id: string | null;
  status: string;
  requested_command: Record<string, string | number | boolean>;
  applied_command: Record<string, string | number | boolean>;
  summary: string;
  planned_for: string;
  executed_at: string;
  details: Record<string, string | number | boolean>;
}

export interface HemsViolationRead {
  id: number;
  asset_key: string | null;
  severity: string;
  violation_type: string;
  message: string;
  details: Record<string, string | number | boolean>;
  created_at: string;
}

export interface HemsPlanRead extends HemsPlanHeaderRead {
  policy: HemsPolicyRead;
  assets: HemsAssetRead[];
  input_snapshot: Record<string, unknown>;
  output_snapshot: Record<string, unknown>;
  intervals: HemsPlanIntervalRead[];
  dispatch_events: HemsDispatchEventRead[];
  violations: HemsViolationRead[];
}

export interface HemsSummaryRead {
  policy: HemsPolicyRead;
  asset_count: number;
  dispatchable_asset_count: number;
  plan_only_asset_count: number;
  blocked_asset_count: number;
  read_only_asset_count: number;
  latest_plan: HemsPlanHeaderRead | null;
}
