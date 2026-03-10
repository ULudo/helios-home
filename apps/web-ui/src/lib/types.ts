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
