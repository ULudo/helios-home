from app.core.config import get_settings
from app.db.models import Site
from app.db.seed import seed_demo_data
from app.db.session import get_engine, get_session_factory, init_database
from app.services.dashboard import build_overview
from app.services.discovery import list_device_candidates, list_discovery_runs, run_discovery
from app.services.discovery_blueprints import RawCandidate
from app.services.local_network import LocalNetworkDiscoveryBatch
from app.services.modbus import ModbusDiscoveryBatch
from app.services.mqtt import MqttDiscoveryBatch
from app.services.network_broadcast import BroadcastDiscoveryBatch
from app.services.recovery import run_recovery


def _build_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    init_database()
    session_factory = get_session_factory()
    session = session_factory()
    seed_demo_data(session)
    return session


def _live_mqtt_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-mqtt-laundry-plug",
        device_id="dev-mqtt-laundry-plug",
        asset_id="asset-mqtt-laundry-plug",
        asset_name="Flexible Smart Load",
        display_name="Laundry Plug",
        manufacturer="Tasmota",
        model="MQTT energy device",
        firmware="unknown",
        device_type="smart_appliance",
        discovery_sources=["mqtt_live"],
        protocols=["mqtt"],
        telemetry={"power_w": 112.0, "energy_today_kwh": 0.8},
        evidence={
            "mqtt_topics": ["tele/laundry-plug/SENSOR", "stat/laundry-plug/POWER"],
            "classification_reasoning": "MQTT topic signature matched a Tasmota smart plug or appliance.",
            "classification_confidence": 0.86,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Imported energy telemetry directly from the configured MQTT broker.",
        next_step_hint="Add a native protocol adapter if write-path validation is required.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _reconcilable_mqtt_grid_meter_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-mqtt-grid-meter-aa-bb",
        device_id="dev-mqtt-grid-meter-aa-bb",
        asset_id="asset-mqtt-grid-meter-aa-bb",
        asset_name="Grid Metering",
        display_name="Grid Meter",
        manufacturer="Shelly",
        model="MQTT energy device",
        firmware="unknown",
        device_type="grid_meter",
        discovery_sources=["mqtt_live"],
        protocols=["mqtt"],
        telemetry={"grid_power_kw": -2.4},
        evidence={
            "mqtt_topics": ["shellies/gridmeter/emeter/0/power"],
            "identity_keys": ["mqtt-slug:shelly-3em-aa-bb"],
            "mqtt_device_slug": "shelly-3em-aa-bb",
            "classification_reasoning": "MQTT topic signature matched a grid meter profile.",
            "classification_confidence": 0.88,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Imported energy telemetry directly from the configured MQTT broker.",
        next_step_hint="Keep the device monitorable through MQTT.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _local_http_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-local-http-shelly-3em-aa-bb",
        device_id="dev-local-http-shelly-3em-aa-bb",
        asset_id="asset-local-http-shelly-3em-aa-bb",
        asset_name="Grid Metering",
        display_name="Grid Meter",
        manufacturer="Shelly",
        model="SHEM-3",
        firmware="2026.2.0",
        device_type="grid_meter",
        discovery_sources=["local_network_live"],
        protocols=["http_local"],
        telemetry={"phase_0_power_w": -812.4, "phase_1_power_w": -790.3, "phase_2_power_w": -801.9},
        evidence={
            "http_base_url": "http://198.51.100.40",
            "http_host": "198.51.100.40",
            "network_macs": ["A1:B2:C3:D4:E5:F6"],
            "identity_keys": ["http-host:198-51-100-40", "network-host:198-51-100-40", "mqtt-slug:shelly-3em-aa-bb"],
            "classification_reasoning": "Local Shelly telemetry exposed multi-phase energy channels and matched the grid_meter profile.",
            "classification_confidence": 0.9,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Helios identified a Shelly local interface and validated a read-only telemetry path.",
        next_step_hint="Keep the device monitorable through the Shelly local API.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _broadcast_grid_meter_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-broadcast-grid-meter",
        device_id="dev-broadcast-grid-meter",
        asset_id="asset-broadcast-grid-meter",
        asset_name="Grid Metering",
        display_name="Shelly Grid Meter Broadcast",
        manufacturer="Shelly",
        model="_http._tcp.local",
        firmware="unknown",
        device_type="grid_meter",
        discovery_sources=["network_broadcast_live"],
        protocols=["mdns", "ssdp"],
        telemetry={},
        evidence={
            "identity_keys": ["http-host:198-51-100-40"],
            "classification_reasoning": "Network broadcast fingerprint matched a grid meter profile.",
            "classification_confidence": 0.82,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Helios identified an energy-relevant local network advertisement.",
        next_step_hint="Use the broadcast evidence to probe a local read path.",
        capabilities_hint={
            "visible": True,
            "monitorable": False,
            "controllable": False,
            "optimizable": False,
        },
    )


def _modbus_grid_meter_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-modbus-grid-meter",
        device_id="dev-modbus-grid-meter",
        asset_id="asset-modbus-grid-meter",
        asset_name="Grid Metering",
        display_name="Shelly EM via Modbus",
        manufacturer="Shelly",
        model="EM-Pro",
        firmware="1.0.0",
        device_type="grid_meter",
        discovery_sources=["modbus_live"],
        protocols=["modbus_tcp"],
        telemetry={"grid_import_total_kwh": 123.4},
        evidence={
            "identity_keys": ["network-host:198-51-100-40"],
            "modbus_host": "198.51.100.40",
            "modbus_unit_id": 1,
            "classification_reasoning": "SunSpec telemetry matched a meter profile.",
            "classification_confidence": 0.89,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Helios validated a native Modbus/TCP endpoint and mapped standardized SunSpec telemetry.",
        next_step_hint="Keep the device monitorable through the native SunSpec read path.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def test_overview_starts_with_seeded_site_only(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        overview = build_overview(session)
        assert overview.site.local_subnet == ""
        assert overview.devices == []
    finally:
        session.close()


def test_discovery_materializes_candidates_devices_and_runs(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        discovery = run_discovery(session)
        assert discovery.candidate_count == 5
        assert discovery.source_names == ["fixture_registry"]
        assert "dev-byd-battery" in discovery.new_device_ids

        overview = build_overview(session)
        assert len(overview.devices) == discovery.integrated_devices
        assert any(device.primary_status == "authentication_required" for device in overview.devices)
        assert len(list_device_candidates(session)) == discovery.candidate_count
        runs = list_discovery_runs(session)
        assert len(runs) == 1
        assert runs[0].new_device_ids == discovery.new_device_ids
        assert runs[0].source_results[0].source_name == "fixture_registry"
    finally:
        session.close()


def test_discovery_rerun_preserves_materialized_devices_and_assets(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        first_run = run_discovery(session)
        first_overview = build_overview(session)
        second_run = run_discovery(session)

        overview = build_overview(session)

        assert first_run.candidate_count == 5
        assert second_run.candidate_count == 5
        assert len(overview.devices) == 5
        assert {device.id for device in overview.devices} == {device.id for device in first_overview.devices}
        assert len(list_device_candidates(session)) == 5
    finally:
        session.close()


def test_discovery_reconciles_candidates_across_native_live_sources(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        site = session.get(Site, 1)
        assert site is not None
        site.local_subnet = "198.51.100.0/24"
        site.mqtt_broker_url = "mqtt://mqtt.example:1883"
        session.add(site)
        session.commit()
        monkeypatch.setenv("HELIOS_LOCAL_SCAN_ENABLED", "true")
        monkeypatch.setenv("HELIOS_BROADCAST_DISCOVERY_ENABLED", "true")
        monkeypatch.setenv("HELIOS_MODBUS_LIVE_ENABLED", "true")
        monkeypatch.setenv("HELIOS_MQTT_LIVE_ENABLED", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(
            "app.services.discovery.discover_local_network_site",
            lambda subnet, timeout_seconds, concurrency, max_hosts: LocalNetworkDiscoveryBatch(
                source_name="local_network_live",
                status="completed",
                message="Imported 1 energy-relevant local HTTP device candidate from subnet scanning.",
                candidates=[_local_http_candidate()],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_network_broadcast",
            lambda timeout_seconds, max_service_types: BroadcastDiscoveryBatch(
                source_name="network_broadcast_live",
                status="completed",
                message="Imported 1 candidate from local network advertisements.",
                candidates=[_broadcast_grid_meter_candidate()],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_modbus_site",
            lambda subnet, timeout_seconds, concurrency, max_hosts: ModbusDiscoveryBatch(
                source_name="modbus_live",
                status="completed",
                message="Imported 1 candidate from native Modbus/TCP probing.",
                candidates=[_modbus_grid_meter_candidate()],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_mqtt_site",
            lambda broker_url, connect_timeout_seconds, probe_window_seconds: MqttDiscoveryBatch(
                source_name="mqtt_live",
                status="completed",
                message="Imported 2 energy-relevant MQTT device candidates.",
                candidates=[_live_mqtt_candidate(), _reconcilable_mqtt_grid_meter_candidate()],
            ),
        )

        discovery = run_discovery(session)
        overview = build_overview(session)

        assert discovery.source_names == [
            "local_network_live",
            "network_broadcast_live",
            "modbus_live",
            "mqtt_live",
        ]
        assert discovery.candidate_count == 2
        assert {device.id for device in overview.devices} == {
            "dev-local-http-shelly-3em-aa-bb",
            "dev-mqtt-laundry-plug",
        }
        reconciled_grid_meter = next(device for device in overview.devices if device.id == "dev-local-http-shelly-3em-aa-bb")
        assert sorted(reconciled_grid_meter.protocols) == ["http_local", "mdns", "modbus_tcp", "mqtt", "ssdp"]
        assert reconciled_grid_meter.telemetry["grid_power_kw"] == -2.4
        assert reconciled_grid_meter.telemetry["phase_0_power_w"] == -812.4
        assert reconciled_grid_meter.telemetry["grid_import_total_kwh"] == 123.4
    finally:
        session.close()


def test_distinct_live_sources_materialize_together_when_not_reconciled(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        site = session.get(Site, 1)
        assert site is not None
        site.local_subnet = "198.51.100.0/24"
        site.mqtt_broker_url = "mqtt://mqtt.example:1883"
        session.add(site)
        session.commit()
        monkeypatch.setenv("HELIOS_LOCAL_SCAN_ENABLED", "true")
        monkeypatch.setenv("HELIOS_BROADCAST_DISCOVERY_ENABLED", "true")
        monkeypatch.setenv("HELIOS_MQTT_LIVE_ENABLED", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(
            "app.services.discovery.discover_local_network_site",
            lambda subnet, timeout_seconds, concurrency, max_hosts: LocalNetworkDiscoveryBatch(
                source_name="local_network_live",
                status="completed",
                message="Imported 1 energy-relevant local HTTP device candidate from subnet scanning.",
                candidates=[_local_http_candidate()],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_network_broadcast",
            lambda timeout_seconds, max_service_types: BroadcastDiscoveryBatch(
                source_name="network_broadcast_live",
                status="completed",
                message="Imported 0 candidates from local network advertisements.",
                candidates=[],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_mqtt_site",
            lambda broker_url, connect_timeout_seconds, probe_window_seconds: MqttDiscoveryBatch(
                source_name="mqtt_live",
                status="completed",
                message="Imported 1 energy-relevant MQTT device candidates.",
                candidates=[_live_mqtt_candidate()],
            ),
        )

        discovery = run_discovery(session)
        overview = build_overview(session)

        assert discovery.source_names == ["local_network_live", "mqtt_live"]
        assert discovery.candidate_count == 2
        assert {device.id for device in overview.devices} == {
            "dev-local-http-shelly-3em-aa-bb",
            "dev-mqtt-laundry-plug",
        }
        assert [result.source_name for result in discovery.source_results] == [
            "local_network_live",
            "network_broadcast_live",
            "mqtt_live",
        ]
    finally:
        session.close()


def test_mqtt_is_used_when_other_live_sources_find_no_candidates(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        site = session.get(Site, 1)
        assert site is not None
        site.local_subnet = "198.51.100.0/24"
        site.mqtt_broker_url = "mqtt://mqtt.example:1883"
        session.add(site)
        session.commit()
        monkeypatch.setenv("HELIOS_LOCAL_SCAN_ENABLED", "true")
        monkeypatch.setenv("HELIOS_BROADCAST_DISCOVERY_ENABLED", "true")
        monkeypatch.setenv("HELIOS_MODBUS_LIVE_ENABLED", "true")
        monkeypatch.setenv("HELIOS_MQTT_LIVE_ENABLED", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(
            "app.services.discovery.discover_local_network_site",
            lambda subnet, timeout_seconds, concurrency, max_hosts: LocalNetworkDiscoveryBatch(
                source_name="local_network_live",
                status="completed",
                message="Local network discovery completed, but no energy-relevant HTTP interfaces were identified.",
                candidates=[],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_network_broadcast",
            lambda timeout_seconds, max_service_types: BroadcastDiscoveryBatch(
                source_name="network_broadcast_live",
                status="completed",
                message="Network broadcast discovery completed, but no energy-relevant advertisements were identified.",
                candidates=[],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_modbus_site",
            lambda subnet, timeout_seconds, concurrency, max_hosts: ModbusDiscoveryBatch(
                source_name="modbus_live",
                status="completed",
                message="Modbus discovery completed, but no native Modbus/TCP devices exposed a usable identity or SunSpec signature.",
                candidates=[],
            ),
        )
        monkeypatch.setattr(
            "app.services.discovery.discover_mqtt_site",
            lambda broker_url, connect_timeout_seconds, probe_window_seconds: MqttDiscoveryBatch(
                source_name="mqtt_live",
                status="completed",
                message="Imported 1 energy-relevant MQTT device candidates.",
                candidates=[_live_mqtt_candidate()],
            ),
        )

        discovery = run_discovery(session)
        overview = build_overview(session)

        assert discovery.source_names == ["mqtt_live"]
        assert [result.source_name for result in discovery.source_results] == [
            "local_network_live",
            "network_broadcast_live",
            "modbus_live",
            "mqtt_live",
        ]
        assert {device.id for device in overview.devices} == {"dev-mqtt-laundry-plug"}
    finally:
        session.close()


def test_local_discovery_combines_multiple_configured_subnets(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("HELIOS_LOCAL_SCAN_ENABLED", "true")
        get_settings.cache_clear()
        site = session.get(Site, 1)
        assert site is not None
        site.local_subnet = "198.51.100.0/24, 203.0.113.0/24"
        session.add(site)
        session.commit()

        seen_subnets: list[str] = []

        def local_batch(subnet, timeout_seconds, concurrency, max_hosts):
            seen_subnets.append(subnet)
            if subnet == "198.51.100.0/24":
                return LocalNetworkDiscoveryBatch(
                    source_name="local_network_live",
                    status="completed",
                    message="Imported 1 local candidate from the first subnet.",
                    candidates=[_local_http_candidate()],
                )
            return LocalNetworkDiscoveryBatch(
                source_name="local_network_live",
                status="completed",
                message="Imported 1 local candidate from the second subnet.",
                candidates=[
                    RawCandidate(
                        candidate_id="cand-local-http-opendtu-lab",
                        device_id="dev-local-http-opendtu-lab",
                        asset_id="asset-local-http-opendtu-lab",
                        asset_name="PV Generation",
                        display_name="OpenDTU Lab",
                        manufacturer="OpenDTU",
                        model="OpenDTU",
                        firmware="2026.3.0",
                        device_type="pv_inverter",
                        discovery_sources=["local_network_live"],
                        protocols=["http_local"],
                        telemetry={"power_w": 1840.0},
                        evidence={
                            "http_host": "203.0.113.25",
                            "classification_reasoning": "Local OpenDTU HTTP endpoint matched a PV inverter profile.",
                            "classification_confidence": 0.88,
                        },
                        recovery_zone="auto_apply",
                        issue_code=None,
                        explanation_hint="Helios validated a local OpenDTU read path.",
                        next_step_hint="Keep the inverter monitorable through the local HTTP endpoint.",
                        capabilities_hint={
                            "visible": True,
                            "monitorable": True,
                            "controllable": False,
                            "optimizable": False,
                        },
                    )
                ],
            )

        monkeypatch.setattr("app.services.discovery.discover_local_network_site", local_batch)

        discovery = run_discovery(session)
        overview = build_overview(session)

        assert seen_subnets == ["198.51.100.0/24", "203.0.113.0/24"]
        assert discovery.source_names == ["local_network_live"]
        assert discovery.candidate_count == 2
        assert discovery.source_results[0].candidate_count == 2
        assert {device.id for device in overview.devices} == {
            "dev-local-http-opendtu-lab",
            "dev-local-http-shelly-3em-aa-bb",
        }
    finally:
        session.close()


def test_failed_live_mqtt_run_records_source_failure_without_fallback(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        run_discovery(session)
        site = session.get(Site, 1)
        assert site is not None
        site.mqtt_broker_url = "mqtt://mqtt.example:1883"
        session.add(site)
        session.commit()
        monkeypatch.setenv("HELIOS_MQTT_LIVE_ENABLED", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(
            "app.services.discovery.discover_mqtt_site",
            lambda broker_url, connect_timeout_seconds, probe_window_seconds: MqttDiscoveryBatch(
                source_name="mqtt_live",
                status="failed",
                message="MQTT connection failed.",
                candidates=[],
            ),
        )

        discovery = run_discovery(session)
        overview = build_overview(session)

        assert discovery.status == "failed"
        assert discovery.source_names == ["mqtt_live"]
        assert discovery.source_results[0].status == "failed"
        assert overview.devices == []
    finally:
        session.close()


def test_guarded_battery_recovery_restores_optimization(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        run_discovery(session)
        recovery = run_recovery(session, "dev-byd-battery")
        assert recovery.agent_run.status == "completed"
        assert recovery.device.primary_status == "optimizable"
        assert recovery.device.capabilities.optimizable is True
    finally:
        session.close()


def test_human_gated_recovery_stays_blocked(tmp_path, monkeypatch):
    session = _build_session(tmp_path, monkeypatch)
    try:
        run_discovery(session)
        recovery = run_recovery(session, "dev-easee-wallbox")
        assert recovery.agent_run.status == "blocked"
        assert recovery.device.primary_status == "authentication_required"
    finally:
        session.close()
