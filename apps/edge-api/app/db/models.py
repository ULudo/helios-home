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
    hems_system_bindings: Mapped[list["HemsSystemBinding"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="HemsSystemBinding.updated_at.desc()",
    )
    setup_profile: Mapped["SiteSetupProfile | None"] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        uselist=False,
    )
    conversation_threads: Mapped[list["ConversationThread"]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        order_by="ConversationThread.updated_at.desc()",
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
    telemetry_status: Mapped[str] = mapped_column(String(40), default="unknown")
    telemetry_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
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


class HemsSystemBinding(Base):
    __tablename__ = "hems_system_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    system_type: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(160))
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="confirmed")
    connection_status: Mapped[str] = mapped_column(String(40), default="unknown")
    telemetry_status: Mapped[str] = mapped_column(String(40), default="unknown")
    control_status: Mapped[str] = mapped_column(String(40), default="unknown")
    source: Mapped[str] = mapped_column(String(80), default="agent")
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="hems_system_bindings")


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


class SiteSetupProfile(Base):
    __tablename__ = "site_setup_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    confirmed_systems: Mapped[list[dict]] = mapped_column(JSON, default=list)
    unresolved_items: Mapped[list[dict]] = mapped_column(JSON, default=list)
    user_notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="setup_profile")


class ConversationThread(Base):
    __tablename__ = "conversation_threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    title: Mapped[str] = mapped_column(String(160), default="Helios setup assistant")
    status: Mapped[str] = mapped_column(String(40), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    site: Mapped[Site] = relationship(back_populates="conversation_threads")
    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at.asc()",
    )
    turns: Mapped[list["ConversationTurn"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ConversationTurn.created_at.asc()",
    )

class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("conversation_threads.id"))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="completed")
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    thread: Mapped[ConversationThread] = relationship(back_populates="messages")
    turn: Mapped["ConversationTurn | None"] = relationship(
        back_populates="assistant_message",
        foreign_keys=[turn_id],
    )


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("conversation_threads.id"))
    user_message_id: Mapped[str] = mapped_column(String(64))
    assistant_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_name: Mapped[str] = mapped_column(String(80), default="stub")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    thread: Mapped[ConversationThread] = relationship(back_populates="turns")
    events: Mapped[list["ConversationEvent"]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
        order_by="ConversationEvent.event_index.asc()",
    )
    assistant_message: Mapped[ConversationMessage | None] = relationship(
        foreign_keys=[assistant_message_id],
        primaryjoin="ConversationTurn.assistant_message_id == ConversationMessage.id",
    )


class ConversationEvent(Base):
    __tablename__ = "conversation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id"))
    event_index: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    turn: Mapped[ConversationTurn] = relationship(back_populates="events")


class HomeGraphEntity(Base):
    __tablename__ = "home_graph_entities"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    entity_type: Mapped[str] = mapped_column(String(60))
    source_type: Mapped[str] = mapped_column(String(60), default="")
    source_id: Mapped[str] = mapped_column(String(96), default="")
    display_name: Mapped[str] = mapped_column(String(180), default="")
    semantic_type: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(60), default="observed")
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ProtocolEndpoint(Base):
    __tablename__ = "protocol_endpoints"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    owner_ref: Mapped[str] = mapped_column(String(96))
    protocol: Mapped[str] = mapped_column(String(80))
    host: Mapped[str] = mapped_column("address", String(180), default="")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_name: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(60), default="observed")
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class EebusLocalIdentity(Base):
    __tablename__ = "eebus_local_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), unique=True)
    common_name: Mapped[str] = mapped_column(String(180), default="Helios Home HEMS")
    ski: Mapped[str] = mapped_column(String(80))
    certificate_pem: Mapped[str] = mapped_column(Text, default="")
    private_key_pem: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ProtocolDiagnosticRun(Base):
    __tablename__ = "protocol_diagnostic_runs"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_threads.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    entity_ref: Mapped[str] = mapped_column(String(96), default="")
    endpoint_ref: Mapped[str] = mapped_column(String(96), default="")
    protocol: Mapped[str] = mapped_column(String(80), default="")
    integration_path: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(60), default="completed")
    log_entries: Mapped[list[dict]] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HomeGraphEvidence(Base):
    __tablename__ = "home_graph_evidence"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    subject_ref: Mapped[str] = mapped_column(String(96))
    evidence_type: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(80), default="system")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(default=0.0)
    trust: Mapped[str] = mapped_column(String(60), default="observed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceAssessment(Base):
    __tablename__ = "device_assessments"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    subject_ref: Mapped[str] = mapped_column(String(96))
    summary: Mapped[str] = mapped_column(Text, default="")
    possible_roles: Mapped[list[dict]] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(60), default="tentative")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_threads.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    task_type: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(180), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(60), default="open")
    target_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskStep(Base):
    __tablename__ = "task_steps"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"))
    step_key: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(60), default="pending")
    summary: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Blocker(Base):
    __tablename__ = "blockers"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True)
    subject_ref: Mapped[str] = mapped_column(String(96), default="")
    blocker_type: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(60), default="open")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_threads.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True)
    proposal_type: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(180), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    target_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(40), default="medium")
    status: Mapped[str] = mapped_column(String(60), default="awaiting_user_decision")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserDecisionRequest(Base):
    __tablename__ = "user_decision_requests"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_threads.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    question: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(40), default="medium")
    status: Mapped[str] = mapped_column(String(60), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserDecision(Base):
    __tablename__ = "user_decisions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("user_decision_requests.id"))
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    decision: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(80), default="user")
    comment: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_threads.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    risk_level: Mapped[str] = mapped_column(String(40), default="low")
    confirmation_policy: Mapped[str] = mapped_column(String(80), default="none")
    status: Mapped[str] = mapped_column(String(60), default="running")
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    output_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentUiEvent(Base):
    __tablename__ = "agent_ui_events"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_threads.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class HemsLoadControlDeviceConfig(Base):
    __tablename__ = "hems_load_control_device_configs"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    receives_lpc: Mapped[bool] = mapped_column(default=False)
    receives_lpp: Mapped[bool] = mapped_column(default=False)
    participates_lpc: Mapped[bool] = mapped_column(default=False)
    participates_lpp: Mapped[bool] = mapped_column(default=False)
    lpc_share_pct: Mapped[float] = mapped_column(default=0.0)
    lpp_share_pct: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class HemsLoadControlLimit(Base):
    __tablename__ = "hems_load_control_limits"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    use_case: Mapped[str] = mapped_column(String(80))
    limit_id: Mapped[int] = mapped_column(default=0)
    direction: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(80), default="eebus")
    peer_ski: Mapped[str] = mapped_column(String(80), default="")
    limit_watts: Mapped[int] = mapped_column(default=0)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HemsLoadControlDelivery(Base):
    __tablename__ = "hems_load_control_deliveries"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    constraint_id: Mapped[str] = mapped_column(ForeignKey("hems_load_control_limits.id"))
    source_peer_ski: Mapped[str] = mapped_column(String(80), default="")
    target_device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    target_endpoint_ref: Mapped[str] = mapped_column(String(96), default="")
    target_peer_ski: Mapped[str] = mapped_column(String(80), default="")
    use_case: Mapped[str] = mapped_column(String(80))
    limit_id: Mapped[int] = mapped_column(default=0)
    limit_watts: Mapped[int] = mapped_column(default=0)
    allocated_limit_watts: Mapped[int] = mapped_column(default=0)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    detail: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    readback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


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
