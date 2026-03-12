from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    local_subnet: Mapped[str] = mapped_column(String(255))
    mqtt_broker_url: Mapped[str] = mapped_column(String(255))
    safety_state: Mapped[str] = mapped_column(String(40), default="safe")
    policy_mode: Mapped[str] = mapped_column(String(40), default="safe")
    discovery_last_run: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    devices: Mapped[list["Device"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    device_candidates: Mapped[list["DeviceCandidate"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    discovery_runs: Mapped[list["DiscoveryRun"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    debug_cases: Mapped[list["DebugCase"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="DebugCase.updated_at.desc()",
    )
    knowledge_entries: Mapped[list["KnowledgeEntry"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="KnowledgeEntry.updated_at.desc()",
    )
    hems_policy: Mapped["HemsPolicy | None"] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        uselist=False,
    )
    hems_plan_runs: Mapped[list["HemsPlanRun"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="HemsPlanRun.created_at.desc()",
    )


class DeviceCandidate(Base):
    __tablename__ = "device_candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    stable_key: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120))
    manufacturer: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(120))
    firmware: Mapped[str] = mapped_column(String(120))
    device_type: Mapped[str] = mapped_column(String(80))
    discovery_sources: Mapped[list[str]] = mapped_column(JSON, default=list)
    protocols: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    classification_confidence: Mapped[float] = mapped_column(default=0.0)
    classification_reasoning: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(40), default="classified")
    matched_device_id: Mapped[str] = mapped_column(String(64), default="")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="device_candidates")


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    status: Mapped[str] = mapped_column(String(40), default="running")
    source_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    integrated_device_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    site: Mapped[Site] = relationship(back_populates="discovery_runs")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    name: Mapped[str] = mapped_column(String(120))
    manufacturer: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(120))
    firmware: Mapped[str] = mapped_column(String(120))
    device_type: Mapped[str] = mapped_column(String(80))
    primary_status: Mapped[str] = mapped_column(String(60))
    status_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(default=0.0)
    recovery_zone: Mapped[str] = mapped_column(String(40))
    protocols: Mapped[list[str]] = mapped_column(JSON, default=list)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    telemetry: Mapped[dict] = mapped_column(JSON, default=dict)
    problem_summary: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    next_step: Mapped[str] = mapped_column(Text, default="")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="devices")
    connector_attempts: Mapped[list["ConnectorAttempt"]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        order_by="ConnectorAttempt.attempted_at.desc()",
    )
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        order_by="Recommendation.created_at.desc()",
    )
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        order_by="Incident.updated_at.desc()",
    )
    agent_runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        order_by="AgentRun.started_at.desc()",
    )


class ConnectorAttempt(Base):
    __tablename__ = "connector_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    connector_name: Mapped[str] = mapped_column(String(120))
    protocol: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    device: Mapped[Device] = relationship(back_populates="connector_attempts")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    name: Mapped[str] = mapped_column(String(120))
    asset_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(60))
    health: Mapped[str] = mapped_column(String(40))
    device_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="assets")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(140))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(40))
    action_type: Mapped[str] = mapped_column(String(60))
    zone: Mapped[str] = mapped_column(String(40))
    auto_applicable: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="recommendations")
    device: Mapped[Device | None] = relationship(back_populates="recommendations")


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    severity: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(140))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="open")
    confidence: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="incidents")
    device: Mapped[Device] = relationship(back_populates="incidents")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    status: Mapped[str] = mapped_column(String(40))
    zone: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    action_plan: Mapped[list[str]] = mapped_column(JSON, default=list)
    rollback_ready: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    device: Mapped[Device] = relationship(back_populates="agent_runs")


class DebugCase(Base):
    __tablename__ = "debug_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    subject_label: Mapped[str] = mapped_column(String(160))
    manufacturer: Mapped[str] = mapped_column(String(120), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    device_type: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="open")
    matched_device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diagnosis_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="debug_cases")
    findings: Mapped[list["ResearchFinding"]] = relationship(
        back_populates="debug_case",
        cascade="all, delete-orphan",
        order_by="ResearchFinding.created_at.desc()",
    )
    probe_runs: Mapped[list["DebugProbeRun"]] = relationship(
        back_populates="debug_case",
        cascade="all, delete-orphan",
        order_by="DebugProbeRun.created_at.desc()",
    )
    knowledge_entries: Mapped[list["KnowledgeEntry"]] = relationship(
        back_populates="source_case",
        order_by="KnowledgeEntry.updated_at.desc()",
    )


class ResearchFinding(Base):
    __tablename__ = "research_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    debug_case_id: Mapped[int] = mapped_column(ForeignKey("debug_cases.id"))
    source_type: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(400), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    debug_case: Mapped[DebugCase] = relationship(back_populates="findings")


class DebugProbeRun(Base):
    __tablename__ = "debug_probe_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    debug_case_id: Mapped[int] = mapped_column(ForeignKey("debug_cases.id"))
    probe_type: Mapped[str] = mapped_column(String(80), default="targeted_probe")
    status: Mapped[str] = mapped_column(String(40), default="completed")
    summary: Mapped[str] = mapped_column(Text, default="")
    checks: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    debug_case: Mapped[DebugCase] = relationship(back_populates="probe_runs")


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    fingerprint_key: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    manufacturer: Mapped[str] = mapped_column(String(120), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    device_type: Mapped[str] = mapped_column(String(80), default="")
    reason_family: Mapped[str] = mapped_column(String(60), default="")
    reason_code: Mapped[str] = mapped_column(String(80), default="")
    feasibility: Mapped[str] = mapped_column(String(80), default="")
    confidence: Mapped[float] = mapped_column(default=0.0)
    summary: Mapped[str] = mapped_column(Text, default="")
    next_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    retrofit_options: Mapped[list[dict]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[dict]] = mapped_column(JSON, default=list)
    raw_diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(60), default="local")
    source_case_id: Mapped[int | None] = mapped_column(ForeignKey("debug_cases.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="knowledge_entries")
    source_case: Mapped[DebugCase | None] = relationship(back_populates="knowledge_entries")


class HemsPolicy(Base):
    __tablename__ = "hems_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), unique=True)
    execution_mode: Mapped[str] = mapped_column(String(40), default="guarded_auto")
    battery_reserve_pct: Mapped[float] = mapped_column(default=20.0)
    ev_default_target_soc_pct: Mapped[float] = mapped_column(default=80.0)
    ev_default_departure_time: Mapped[str] = mapped_column(String(16), default="07:00")
    heat_comfort_min_c: Mapped[float] = mapped_column(default=20.0)
    heat_comfort_max_c: Mapped[float] = mapped_column(default=22.5)
    grid_import_limit_kw: Mapped[float] = mapped_column(default=12.0)
    grid_export_limit_kw: Mapped[float] = mapped_column(default=12.0)
    allow_price_arbitrage: Mapped[bool] = mapped_column(default=True)
    allow_heat_precharge: Mapped[bool] = mapped_column(default=True)
    allow_ev_load_shifting: Mapped[bool] = mapped_column(default=True)
    horizon_hours: Mapped[int] = mapped_column(default=24)
    step_minutes: Mapped[int] = mapped_column(default=15)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="hems_policy")


class HemsPlanRun(Base):
    __tablename__ = "hems_plan_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    status: Mapped[str] = mapped_column(String(40), default="running")
    execution_mode: Mapped[str] = mapped_column(String(40), default="guarded_auto")
    triggered_by: Mapped[str] = mapped_column(String(80), default="manual")
    solver_name: Mapped[str] = mapped_column(String(80), default="cvxpy-highs")
    objective_value: Mapped[float | None] = mapped_column(nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    output_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    horizon_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    horizon_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    site: Mapped[Site] = relationship(back_populates="hems_plan_runs")
    intervals: Mapped[list["HemsPlanInterval"]] = relationship(
        back_populates="plan_run",
        cascade="all, delete-orphan",
        order_by="HemsPlanInterval.starts_at.asc()",
    )
    dispatch_events: Mapped[list["HemsDispatchEvent"]] = relationship(
        back_populates="plan_run",
        cascade="all, delete-orphan",
        order_by="HemsDispatchEvent.executed_at.asc()",
    )
    violations: Mapped[list["HemsViolation"]] = relationship(
        back_populates="plan_run",
        cascade="all, delete-orphan",
        order_by="HemsViolation.created_at.asc()",
    )


class HemsPlanInterval(Base):
    __tablename__ = "hems_plan_intervals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_run_id: Mapped[str] = mapped_column(ForeignKey("hems_plan_runs.id"))
    asset_key: Mapped[str] = mapped_column(String(120))
    asset_type: Mapped[str] = mapped_column(String(80))
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    command: Mapped[dict] = mapped_column(JSON, default=dict)
    predicted_state: Mapped[dict] = mapped_column(JSON, default=dict)

    plan_run: Mapped[HemsPlanRun] = relationship(back_populates="intervals")


class HemsDispatchEvent(Base):
    __tablename__ = "hems_dispatch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_run_id: Mapped[str] = mapped_column(ForeignKey("hems_plan_runs.id"))
    asset_key: Mapped[str] = mapped_column(String(120))
    asset_type: Mapped[str] = mapped_column(String(80))
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="skipped")
    requested_command: Mapped[dict] = mapped_column(JSON, default=dict)
    applied_command: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    planned_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    plan_run: Mapped[HemsPlanRun] = relationship(back_populates="dispatch_events")


class HemsViolation(Base):
    __tablename__ = "hems_violations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_run_id: Mapped[str] = mapped_column(ForeignKey("hems_plan_runs.id"))
    asset_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    severity: Mapped[str] = mapped_column(String(40), default="warning")
    violation_type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plan_run: Mapped[HemsPlanRun] = relationship(back_populates="violations")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
