import type { DiscoveryRunRead, OverviewResponse, ReachableSubnetRead, SiteRead, SiteUpdate } from "./types";

const apiBase = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
}

export const api = {
  getOverview: () => request<OverviewResponse>("/overview"),
  listReachableSubnets: () => request<ReachableSubnetRead[]>("/network/reachable-subnets"),
  runDiscovery: () => request<DiscoveryRunRead>("/discovery/runs", { method: "POST" }),
  updateSite: (payload: SiteUpdate) =>
    request<SiteRead>("/site", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
};
