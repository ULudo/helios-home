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

export interface HemsCommandContractRead {
  command_key: string;
  value_type: string;
  unit: string | null;
  minimum: number | null;
  maximum: number | null;
  allowed_values: string[];
  adapter_name: string | null;
  validation_state: string;
  requires_native_writes: boolean;
  safety_checks: string[];
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
  command_contract: HemsCommandContractRead | null;
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

export interface SetupSystemBindingRead {
  system_type: string;
  label: string;
  device_id: string | null;
  device_name: string | null;
  status: string;
}

export interface SetupItemRead {
  kind: string;
  label: string;
  details: string;
  status: string;
}

export interface SiteSetupProfileRead {
  summary: string;
  confirmed_systems: SetupSystemBindingRead[];
  unresolved_items: SetupItemRead[];
  user_notes: string[];
  updated_at: string;
}

export interface AgentProviderOptionRead {
  provider_id: string;
  label: string;
  description: string;
  auth_kind: string;
  base_url_default: string | null;
  model_placeholder: string;
  supports_base_url: boolean;
  supports_model: boolean;
  selected: boolean;
  model: string;
  base_url: string | null;
  api_key_configured: boolean;
  ready: boolean;
}

export interface AgentProviderConfigRead {
  selected_provider: string;
  effective_provider: string;
  ready: boolean;
  message: string;
  provider_options: AgentProviderOptionRead[];
}

export interface AgentProviderConfigUpdate {
  provider_id: string;
  model?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  clear_api_key?: boolean;
}

export interface AgentMessageRead {
  id: string;
  role: string;
  content: string;
  status: string;
  created_at: string;
  turn_id: string | null;
}

export interface ActionProposalRead {
  id: string;
  action_type: string;
  summary: string;
  payload: Record<string, unknown>;
  status: string;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

export interface DebugEvidenceRead {
  kind: string;
  label: string;
  value: string;
  source: string;
  confidence: number | null;
}

export interface RetrofitOptionRead {
  kind: string;
  label: string;
  description: string;
  effort: string;
  requires_electrician: boolean;
  requires_vendor_gateway: boolean;
}

export interface DebugDiagnosisRead {
  state: string;
  reason_family: string;
  reason_code: string;
  feasibility: string;
  confidence: number;
  summary: string;
  evidence: DebugEvidenceRead[];
  next_actions: string[];
  retrofit_options: RetrofitOptionRead[];
  raw_diagnostics: Record<string, unknown>;
}

export interface DebugCaseRead {
  id: number;
  subject_label: string;
  manufacturer: string;
  model: string;
  device_type: string;
  notes: string;
  status: string;
  matched_device_id: string | null;
  matched_candidate_id: string | null;
  diagnosis: DebugDiagnosisRead;
  created_at: string;
  updated_at: string;
}

export interface AgentThreadRead {
  id: string;
  title: string;
  status: string;
  messages: AgentMessageRead[];
  pending_proposals: ActionProposalRead[];
  setup_profile: SiteSetupProfileRead;
  latest_debug_case: DebugCaseRead | null;
  created_at: string;
  updated_at: string;
}

export interface AgentMessageCreate {
  content: string;
}

export interface AgentTurnAcceptedRead {
  thread_id: string;
  turn_id: string;
  user_message: AgentMessageRead;
}

export interface AgentTurnEventRead {
  turn_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ActionProposalDecisionRead {
  proposal: ActionProposalRead;
  thread: AgentThreadRead;
}

export type ViewKey = "overview" | "devices" | "monitoring" | "tasks" | "settings";
export type NavigationMode = "peek" | "focus" | "switch";
export type TimeRange = "last_1h" | "last_6h" | "last_24h" | "last_7d";

export type AgentUiAction =
  | { type: "open_view"; payload: { view: ViewKey; mode?: NavigationMode } }
  | { type: "focus_system"; payload: { system_type: string | null } }
  | { type: "select_devices"; payload: { device_ids: string[] } }
  | { type: "highlight_devices"; payload: { device_ids: string[] } }
  | {
      type: "show_monitoring";
      payload: { device_ids: string[]; metric_keys: string[]; time_range: TimeRange; mode?: NavigationMode };
    }
  | {
      type: "show_explanation";
      payload: { title: string; body: string; severity?: "info" | "caution" | "critical" };
    }
  | { type: "clear_focus"; payload: Record<string, never> };
