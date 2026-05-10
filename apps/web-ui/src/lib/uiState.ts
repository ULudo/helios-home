import type { AgentUiEvent, NavigationMode, TimeRange, ViewKey } from "./types";

export type ExplanationCard = {
  title: string;
  body: string;
  severity: "info" | "caution" | "critical";
} | null;

export type ActiveTaskHint = {
  taskRef: string;
  mode: "progress" | "blockers" | "summary";
  title: string;
  status: string;
  summary: string;
  blockers: Array<Record<string, unknown>>;
} | null;

export type UIState = {
  currentView: ViewKey;
  navigationMode: NavigationMode | null;
  focusedSystem: string | null;
  selectedDeviceIds: string[];
  highlightedDeviceIds: string[];
  monitoring: {
    deviceIds: string[];
    metricKeys: string[];
    timeRange: TimeRange;
  };
  explanation: ExplanationCard;
  activeTask: ActiveTaskHint;
  viewLock: boolean;
  lastAgentNavigationAt: string | null;
};

type UIStateEffect =
  | { type: "open_view"; payload: { view: ViewKey; mode?: NavigationMode } }
  | { type: "focus_system"; payload: { system_type: string | null } }
  | { type: "select_devices"; payload: { device_ids: string[] } }
  | { type: "highlight_devices"; payload: { device_ids: string[] } }
  | {
      type: "show_monitoring";
      payload: { device_ids: string[]; metric_keys: string[]; time_range: TimeRange; mode?: NavigationMode };
    }
  | {
      type: "show_task";
      payload: {
        task_ref: string;
        mode?: "progress" | "blockers" | "summary";
        title?: string;
        status?: string;
        summary?: string;
        blockers?: Array<Record<string, unknown>>;
      };
    }
  | { type: "clear_focus"; payload: Record<string, never> };

export type UIStateAction =
  | { type: "apply_actions"; actions: UIStateEffect[]; occurredAt: string }
  | { type: "set_view"; view: ViewKey }
  | { type: "toggle_view_lock" }
  | { type: "toggle_device_selection"; deviceId: string }
  | { type: "set_monitoring_metric"; metricKey: string }
  | { type: "set_time_range"; timeRange: TimeRange }
  | { type: "clear_explanation" };

export function createInitialUIState(): UIState {
  return {
    currentView: "overview",
    navigationMode: null,
    focusedSystem: null,
    selectedDeviceIds: [],
    highlightedDeviceIds: [],
    monitoring: {
      deviceIds: [],
      metricKeys: ["power_w"],
      timeRange: "last_24h",
    },
    explanation: null,
    activeTask: null,
    viewLock: false,
    lastAgentNavigationAt: null,
  };
}

function normalizeSupportedView(view: ViewKey): ViewKey {
  return view === "settings" ? "settings" : "overview";
}

function applySingleUiAction(state: UIState, action: UIStateEffect, occurredAt: string): UIState {
  if (action.type === "open_view") {
    const mode = action.payload.mode ?? "focus";
    if (mode === "peek") {
      return {
        ...state,
        navigationMode: mode,
        lastAgentNavigationAt: occurredAt,
      };
    }
    if (state.viewLock) {
      return {
        ...state,
        navigationMode: mode,
        lastAgentNavigationAt: occurredAt,
      };
    }
    return {
      ...state,
      currentView: normalizeSupportedView(action.payload.view),
      navigationMode: mode,
      lastAgentNavigationAt: occurredAt,
    };
  }

  if (action.type === "focus_system") {
    return {
      ...state,
      focusedSystem: action.payload.system_type,
    };
  }

  if (action.type === "select_devices") {
    return {
      ...state,
      selectedDeviceIds: [...action.payload.device_ids],
      monitoring: {
        ...state.monitoring,
        deviceIds: [...action.payload.device_ids],
      },
    };
  }

  if (action.type === "highlight_devices") {
    return {
      ...state,
      highlightedDeviceIds: [...action.payload.device_ids],
    };
  }

  if (action.type === "show_monitoring") {
    const mode = action.payload.mode ?? "focus";
    const nextState: UIState = {
      ...state,
      monitoring: {
        deviceIds: [...action.payload.device_ids],
        metricKeys: [...action.payload.metric_keys],
        timeRange: action.payload.time_range,
      },
      selectedDeviceIds: [...action.payload.device_ids],
      highlightedDeviceIds: [...action.payload.device_ids],
      navigationMode: mode,
      lastAgentNavigationAt: occurredAt,
    };
    if (mode !== "peek" && !state.viewLock) {
      nextState.currentView = "overview";
    }
    return nextState;
  }

  if (action.type === "show_task") {
    return {
      ...state,
      activeTask: {
        taskRef: action.payload.task_ref,
        mode: action.payload.mode ?? "summary",
        title: action.payload.title ?? "",
        status: action.payload.status ?? "",
        summary: action.payload.summary ?? "",
        blockers: action.payload.blockers ?? [],
      },
    };
  }

  if (action.type === "clear_focus") {
    return {
      ...state,
      focusedSystem: null,
      selectedDeviceIds: [],
      highlightedDeviceIds: [],
    };
  }

  return state;
}

export function uiStateReducer(state: UIState, action: UIStateAction): UIState {
  if (action.type === "apply_actions") {
    return action.actions.reduce((current, nextAction) => applySingleUiAction(current, nextAction, action.occurredAt), state);
  }

  if (action.type === "set_view") {
    return {
      ...state,
      currentView: normalizeSupportedView(action.view),
      navigationMode: "switch",
    };
  }

  if (action.type === "toggle_view_lock") {
    return {
      ...state,
      viewLock: !state.viewLock,
    };
  }

  if (action.type === "toggle_device_selection") {
    const selected = state.selectedDeviceIds.includes(action.deviceId)
      ? state.selectedDeviceIds.filter((entry) => entry !== action.deviceId)
      : [...state.selectedDeviceIds, action.deviceId];
    return {
      ...state,
      selectedDeviceIds: selected,
      monitoring: {
        ...state.monitoring,
        deviceIds: selected,
      },
    };
  }

  if (action.type === "set_monitoring_metric") {
    return {
      ...state,
      monitoring: {
        ...state.monitoring,
        metricKeys: [action.metricKey],
      },
    };
  }

  if (action.type === "set_time_range") {
    return {
      ...state,
      monitoring: {
        ...state.monitoring,
        timeRange: action.timeRange,
      },
    };
  }

  if (action.type === "clear_explanation") {
    return {
      ...state,
      explanation: null,
    };
  }

  return state;
}

function deviceIdsFromEntityRefs(entityRefs: string[]): string[] {
  return entityRefs
    .map((ref) => (ref.startsWith("device:") ? ref.slice("device:".length) : ""))
    .filter(Boolean);
}

function uiEventToActions(event: AgentUiEvent): UIStateEffect[] {
  if (event.event_type === "view.open") {
    return [{ type: "open_view", payload: event.payload }];
  }
  if (event.event_type === "entity.focus") {
    const deviceIds = deviceIdsFromEntityRefs(event.payload.entity_refs);
    if (event.payload.mode === "highlight") {
      return deviceIds.length > 0 ? [{ type: "highlight_devices", payload: { device_ids: deviceIds } }] : [];
    }
    return deviceIds.length > 0
      ? [
          { type: "open_view", payload: { view: "overview", mode: "focus" } },
          { type: "select_devices", payload: { device_ids: deviceIds } },
          { type: "highlight_devices", payload: { device_ids: deviceIds } },
        ]
      : [];
  }
  if (event.event_type === "assessment.show") {
    const deviceIds = deviceIdsFromEntityRefs([event.payload.entity_ref]);
    return deviceIds.length > 0
      ? [
          { type: "open_view", payload: { view: "overview", mode: "focus" } },
          { type: "select_devices", payload: { device_ids: deviceIds } },
          { type: "highlight_devices", payload: { device_ids: deviceIds } },
        ]
      : [];
  }
  if (event.event_type === "task.show") {
    return [{ type: "show_task", payload: event.payload }];
  }
  return [];
}

export function parseUiEvents(input: unknown): UIStateEffect[] {
  if (!Array.isArray(input)) {
    return [];
  }
  return input
    .filter((entry): entry is AgentUiEvent => {
      if (typeof entry !== "object" || entry === null) {
        return false;
      }
      const eventType = (entry as { event_type?: unknown }).event_type;
      return (
        eventType === "view.open" ||
        eventType === "entity.focus" ||
        eventType === "device.details.open" ||
        eventType === "connection.overlay.open" ||
        eventType === "entity.relationship.show" ||
        eventType === "task.show" ||
        eventType === "proposal.present" ||
        eventType === "evidence.recorded" ||
        eventType === "assessment.show"
      );
    })
    .flatMap((event) => uiEventToActions(event));
}
