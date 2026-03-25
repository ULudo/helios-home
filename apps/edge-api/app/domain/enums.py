from enum import Enum


class IntegrationStatus(str, Enum):
    DISCOVERED = "discovered"
    CONNECTED = "connected"
    VISIBLE_ONLY = "visible_only"
    MONITORABLE = "monitorable"
    PARTIALLY_INTEGRABLE = "partially_integrable"
    CONTROLLABLE = "controllable"
    OPTIMIZABLE = "optimizable"
    AUTHENTICATION_REQUIRED = "authentication_required"
    MANUFACTURER_ACCESS_REQUIRED = "manufacturer_access_required"
    PROTOCOL_INCOMPLETE = "protocol_incomplete"
    NOT_INTEGRATABLE = "not_integratable"
    IN_ANALYSIS = "in_analysis"
    RECOVERY_RUNNING = "recovery_running"


class RecoveryZone(str, Enum):
    AUTO_APPLY = "auto_apply"
    GUARDED_APPLY = "guarded_apply"
    HUMAN_GATED = "human_gated"


class ConnectorOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    INFO = "info"


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    PROPOSAL_READY = "proposal_ready"
    FAILED = "failed"


class DiscoveryRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ProbeRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class ProbeCheckOutcome(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class ExplainabilityState(str, Enum):
    INTEGRATED = "integrated"
    NOT_FOUND = "not_found"
    SEEN_BUT_NOT_CLASSIFIED = "seen_but_not_classified"
    CLASSIFIED_BUT_NOT_INTEGRABLE = "classified_but_not_integrable"


class ExplainabilityReasonFamily(str, Enum):
    OPERATIONAL = "operational"
    NETWORK = "network"
    AUTH = "auth"
    PROTOCOL = "protocol"
    INTERFACE = "interface"
    CLASSIFICATION = "classification"
    VENDOR = "vendor"
    RETROFIT = "retrofit"
    UNKNOWN = "unknown"


class ExplainabilityReasonCode(str, Enum):
    VALIDATED_INTERFACE = "validated_interface"
    NO_MATCH_IN_DISCOVERY = "no_match_in_discovery"
    NETWORK_UNREACHABLE = "network_unreachable"
    NO_SUPPORTED_INTERFACE = "no_supported_interface"
    AUTH_REQUIRED = "auth_required"
    GATEWAY_REQUIRED = "gateway_required"
    PROTOCOL_INCOMPLETE = "protocol_incomplete"
    TELEMETRY_PATH_NOT_VALIDATED = "telemetry_path_not_validated"
    CLASSIFICATION_CONFIDENCE_LOW = "classification_confidence_low"
    RETROFIT_POSSIBLE = "retrofit_possible"
    UNKNOWN = "unknown"


class IntegrationFeasibility(str, Enum):
    NETWORK_NATIVE = "network_native"
    NETWORK_NATIVE_BUT_AUTH_BLOCKED = "network_native_but_auth_blocked"
    NETWORK_NATIVE_BUT_UNSUPPORTED = "network_native_but_unsupported"
    GATEWAY_POSSIBLE = "gateway_possible"
    DRY_CONTACT_POSSIBLE = "dry_contact_possible"
    METER_ONLY_POSSIBLE = "meter_only_possible"
    NOT_REASONABLY_INTEGRABLE = "not_reasonably_integrable"
    UNKNOWN = "unknown"


class HemsExecutionMode(str, Enum):
    GUARDED_AUTO = "guarded_auto"
    PLAN_ONLY = "plan_only"


class HemsAssetType(str, Enum):
    GRID_METER = "grid_meter"
    PV_INVERTER = "pv_inverter"
    BATTERY = "battery"
    EV_CHARGER = "ev_charger"
    HEAT_PUMP = "heat_pump"
    CONTROLLABLE_LOAD = "controllable_load"
    UNCONTROLLED_LOAD = "uncontrolled_load"
    TARIFF_SOURCE = "tariff_source"
    WEATHER_SOURCE = "weather_source"


class HemsControlCapability(str, Enum):
    MONITOR_ONLY = "monitor_only"
    SET_POWER = "set_power"
    SET_CURRENT = "set_current"
    SET_MODE = "set_mode"
    START_STOP = "start_stop"


class HemsEligibility(str, Enum):
    READ_ONLY = "read_only"
    PLAN_ONLY = "plan_only"
    DISPATCHABLE = "dispatchable"
    BLOCKED = "blocked"


class HemsPlanStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"


class HemsDispatchStatus(str, Enum):
    APPLIED = "applied"
    SIMULATED = "simulated"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


class HemsViolationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
