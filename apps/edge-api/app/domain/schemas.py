from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SiteRead(BaseModel):
    id: int
    local_subnet: str
    updated_at: datetime


class SiteUpdate(BaseModel):
    local_subnet: str | None = None


class ReachableSubnetRead(BaseModel):
    cidr: str
    interface: str
    label: str


class CapabilityRead(BaseModel):
    visible: bool
    monitorable: bool
    controllable: bool
    optimizable: bool


class DeviceLoadControlRead(BaseModel):
    receives_lpc: bool = False
    receives_lpp: bool = False
    participates_lpc: bool = False
    participates_lpp: bool = False
    lpc_share_pct: float = 0.0
    lpp_share_pct: float = 0.0


class ConnectorAttemptRead(BaseModel):
    id: int
    connector_name: str
    protocol: str
    outcome: str
    detail: str
    attempted_at: datetime


class DeviceCandidateRead(BaseModel):
    id: str
    stable_key: str
    display_name: str
    manufacturer: str
    model: str
    firmware: str
    device_type: str
    discovery_sources: list[str]
    protocols: list[str]
    evidence: dict[str, Any]
    classification_confidence: float
    classification_reasoning: str
    state: str
    matched_device_id: str
    last_seen_at: datetime


class DebugEvidenceRead(BaseModel):
    kind: str
    label: str
    value: str
    source: str
    confidence: float | None = None


class RetrofitOptionRead(BaseModel):
    kind: str
    label: str
    description: str
    effort: str
    requires_electrician: bool = False
    requires_vendor_gateway: bool = False


class DebugDiagnosisRead(BaseModel):
    state: str
    reason_family: str
    reason_code: str
    feasibility: str
    confidence: float
    summary: str
    evidence: list[DebugEvidenceRead] = Field(default_factory=list)
    retrofit_options: list[RetrofitOptionRead] = Field(default_factory=list)
    raw_diagnostics: dict[str, Any] = Field(default_factory=dict)


class DebugReportRead(BaseModel):
    subject_type: str
    subject_id: str | None = None
    subject_label: str
    matched_device_id: str | None = None
    matched_candidate_id: str | None = None
    diagnosis: DebugDiagnosisRead


class DebugExplainRequest(BaseModel):
    manufacturer: str = ""
    model: str = ""
    device_type: str = ""
    notes: str = ""


class ResearchFindingCreate(BaseModel):
    source_type: str
    title: str
    summary: str
    url: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ResearchFindingRead(BaseModel):
    id: int
    source_type: str
    title: str
    summary: str
    url: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProbeCheckRead(BaseModel):
    name: str
    outcome: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None


class DebugProbeRunRead(BaseModel):
    id: int
    probe_type: str
    status: str
    summary: str
    checks: list[ProbeCheckRead] = Field(default_factory=list)
    created_at: datetime


class DebugCaseRead(BaseModel):
    id: int
    subject_label: str
    manufacturer: str
    model: str
    device_type: str
    notes: str
    status: str
    matched_device_id: str | None = None
    matched_candidate_id: str | None = None
    diagnosis: DebugDiagnosisRead
    findings: list[ResearchFindingRead] = Field(default_factory=list)
    probe_runs: list[DebugProbeRunRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class KnowledgeEntryRead(BaseModel):
    id: int
    fingerprint_key: str
    title: str
    manufacturer: str
    model: str
    device_type: str
    reason_family: str
    reason_code: str
    feasibility: str
    confidence: float
    summary: str
    retrofit_options: list[RetrofitOptionRead] = Field(default_factory=list)
    evidence: list[DebugEvidenceRead] = Field(default_factory=list)
    raw_diagnostics: dict[str, Any] = Field(default_factory=dict)
    origin: str
    source_case_id: int | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgePackRead(BaseModel):
    exported_at: datetime
    entries: list[KnowledgeEntryRead] = Field(default_factory=list)


class KnowledgeEntryWrite(BaseModel):
    fingerprint_key: str
    title: str
    manufacturer: str = ""
    model: str = ""
    device_type: str = ""
    reason_family: str
    reason_code: str
    feasibility: str
    confidence: float
    summary: str
    retrofit_options: list[RetrofitOptionRead] = Field(default_factory=list)
    evidence: list[DebugEvidenceRead] = Field(default_factory=list)
    raw_diagnostics: dict[str, Any] = Field(default_factory=dict)
    origin: str = "import"


class KnowledgePackWrite(BaseModel):
    entries: list[KnowledgeEntryWrite] = Field(default_factory=list)


class KnowledgeImportResultRead(BaseModel):
    imported_count: int
    updated_count: int
    total_entries: int


class DeviceRead(BaseModel):
    id: str
    name: str
    manufacturer: str
    model: str
    firmware: str
    device_type: str
    primary_status: str
    status_tags: list[str]
    confidence: float
    recovery_zone: str
    protocols: list[str]
    capabilities: CapabilityRead
    load_control: DeviceLoadControlRead = Field(default_factory=DeviceLoadControlRead)
    telemetry: dict[str, Any]
    last_seen_at: datetime
    connector_attempts: list[ConnectorAttemptRead] = Field(default_factory=list)


class AssetRead(BaseModel):
    id: str
    name: str
    asset_type: str
    status: str
    health: str
    device_ids: list[str]
    metrics: dict[str, Any]


class IncidentRead(BaseModel):
    id: int
    device_id: str
    severity: str
    title: str
    summary: str
    status: str
    confidence: float
    created_at: datetime
    updated_at: datetime


class AgentRunRead(BaseModel):
    id: str
    device_id: str
    status: str
    zone: str
    summary: str
    action_plan: list[str]
    rollback_ready: bool
    started_at: datetime
    finished_at: datetime | None


class AuditEventRead(BaseModel):
    id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    summary: str
    details: dict[str, Any]
    created_at: datetime


class DiscoverySourceResultRead(BaseModel):
    source_name: str
    status: str
    message: str
    candidate_count: int


class DiscoveryRunRead(BaseModel):
    id: str | None = None
    status: str | None = None
    source_names: list[str] = Field(default_factory=list)
    source_results: list[DiscoverySourceResultRead] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    executed_at: datetime
    message: str
    new_device_ids: list[str]
    refreshed_devices: int
    candidate_count: int = 0
    integrated_devices: int = 0


class RecoveryRunRead(BaseModel):
    message: str
    device: DeviceRead
    agent_run: AgentRunRead


class OverviewResponse(BaseModel):
    site: SiteRead
    devices: list[DeviceRead]
