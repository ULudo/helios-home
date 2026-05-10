import type {
  ActionProposalDecisionRead,
  ActionExecuteRequest,
  ActionExecutionRead,
  AgentMessageCreate,
  AgentProviderConfigRead,
  AgentProviderConfigUpdate,
  AgentThreadRead,
  AgentTurnAcceptedRead,
  AgentTurnEventRead,
  ConnectionOptionsRead,
  ConnectionStateRead,
  DeviceRead,
  DiscoveryRunRead,
  HemsAssetRead,
  HemsPlanRead,
  HemsPolicyRead,
  HemsPolicyUpdate,
  HemsSummaryRead,
  OverviewResponse,
  ReachableSubnetRead,
  SiteSetupProfileRead,
  SiteRead,
  SiteUpdate,
  UserDecisionCreate,
} from "./types";

const apiBase = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "/api/v1";

function normalizeRequestError(error: unknown): Error {
  if (error instanceof Error) {
    if (error.name === "TypeError") {
      return new Error(
        "Helios backend is not reachable. Start the backend on http://127.0.0.1:8000 or set VITE_API_BASE to a running API.",
      );
    }
    return error;
  }
  return new Error("Unable to reach the Helios backend.");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    const response = await fetch(`${apiBase}${path}`, {
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      ...init,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || `Request failed with ${response.status}`);
    }

    return (await response.json()) as T;
  } catch (error) {
    throw normalizeRequestError(error);
  }
}

async function requestOrNull<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const response = await fetch(`${apiBase}${path}`, {
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      ...init,
    });

    if (response.status === 404) {
      return null;
    }

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || `Request failed with ${response.status}`);
    }

    return (await response.json()) as T;
  } catch (error) {
    throw normalizeRequestError(error);
  }
}

export const api = {
  getAgentThread: () => request<AgentThreadRead>("/agent/thread"),
  getAgentProviderConfig: () => request<AgentProviderConfigRead>("/agent/provider-config"),
  updateAgentProviderConfig: (payload: AgentProviderConfigUpdate) =>
    request<AgentProviderConfigRead>("/agent/provider-config", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getAgentSetupProfile: () => request<SiteSetupProfileRead>("/agent/setup-profile"),
  createAgentMessage: (payload: AgentMessageCreate) =>
    request<AgentTurnAcceptedRead>("/agent/messages", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  respondToDecisionRequest: (decisionRequestId: string, payload: UserDecisionCreate) =>
    request<ActionProposalDecisionRead>(`/agent/decision-requests/${decisionRequestId}/responses`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  executeAction: (actionName: string, payload: ActionExecuteRequest) =>
    request<ActionExecutionRead>(`/actions/${actionName}`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getDeviceConnectionOptions: (deviceId: string) => request<ConnectionOptionsRead>(`/devices/${deviceId}/connection-options`),
  getConnectionState: (params: { entity_ref: string; endpoint_ref: string; integration_path: string }) => {
    const query = new URLSearchParams({
      entity_ref: params.entity_ref,
      endpoint_ref: params.endpoint_ref,
      integration_path: params.integration_path,
    });
    return request<ConnectionStateRead>(`/connections/state?${query.toString()}`);
  },
  removeDevice: (deviceId: string) => request<DeviceRead>(`/devices/${deviceId}`, { method: "DELETE" }),
  getOverview: () => request<OverviewResponse>("/overview"),
  listReachableSubnets: () => request<ReachableSubnetRead[]>("/network/reachable-subnets"),
  runDiscovery: () => request<DiscoveryRunRead>("/discovery/runs", { method: "POST" }),
  updateSite: (payload: SiteUpdate) =>
    request<SiteRead>("/site", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getHemsSummary: () => request<HemsSummaryRead>("/hems/summary"),
  listHemsAssets: () => request<HemsAssetRead[]>("/hems/assets"),
  getLatestHemsPlan: () => requestOrNull<HemsPlanRead>("/hems/plans/latest"),
  updateHemsPolicy: (payload: HemsPolicyUpdate) =>
    request<HemsPolicyRead>("/hems/policy", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  runHemsReplan: () => request<HemsPlanRead>("/hems/replan", { method: "POST" }),
  streamAgentTurn: (
    turnId: string,
    handlers: {
      onEvent: (event: AgentTurnEventRead) => void;
      onError?: (error: Error) => void;
      onEnd?: () => void;
    },
  ) => {
    const source = new EventSource(`${apiBase}/agent/turns/${turnId}/events`);
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as AgentTurnEventRead;
        handlers.onEvent(payload);
        if (payload.event_type === "stream_end") {
          source.close();
          handlers.onEnd?.();
        }
      } catch (error) {
        source.close();
        handlers.onError?.(error instanceof Error ? error : new Error("Unable to decode agent stream."));
      }
    };
    source.onerror = () => {
      source.close();
      handlers.onError?.(new Error("The agent stream disconnected."));
    };
    return source;
  },
};
