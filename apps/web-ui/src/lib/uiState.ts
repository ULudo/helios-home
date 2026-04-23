import type { AgentUiAction, NavigationMode, TimeRange, ViewKey } from "./types";

export type ExplanationCard = {
  title: string;
  body: string;
  severity: "info" | "caution" | "critical";
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
  viewLock: boolean;
  lastAgentNavigationAt: string | null;
};

export type UIStateAction =
  | { type: "apply_actions"; actions: AgentUiAction[]; occurredAt: string }
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
    viewLock: false,
    lastAgentNavigationAt: null,
  };
}

function normalizeSupportedView(view: ViewKey): ViewKey {
  return view === "settings" ? "settings" : "overview";
}

function applySingleUiAction(state: UIState, action: AgentUiAction, occurredAt: string): UIState {
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

  if (action.type === "show_explanation") {
    return {
      ...state,
      explanation: {
        title: action.payload.title,
        body: action.payload.body,
        severity: action.payload.severity ?? "info",
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

export function parseUiActions(input: unknown): AgentUiAction[] {
  if (!Array.isArray(input)) {
    return [];
  }
  return input
    .filter((entry): entry is { type: string; payload?: Record<string, unknown> } => {
      return typeof entry === "object" && entry !== null && typeof (entry as { type?: unknown }).type === "string";
    })
    .map((entry) => ({
      type: entry.type,
      payload: (entry.payload ?? {}) as AgentUiAction["payload"],
    })) as AgentUiAction[];
}
