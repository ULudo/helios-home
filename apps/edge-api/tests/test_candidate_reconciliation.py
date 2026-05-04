from app.services.candidate_reconciliation import reconcile_candidates
from app.services.discovery_blueprints import RawCandidate


def _local_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-local",
        device_id="dev-local",
        asset_id="asset-local",
        asset_name="Grid Metering",
        display_name="Grid Meter",
        manufacturer="Shelly",
        model="SHEM-3",
        firmware="2026.2",
        device_type="grid_meter",
        discovery_sources=["local_network_live"],
        protocols=["http_local"],
        telemetry={"phase_0_power_w": -810.1},
        evidence={
            "network_macs": ["A1:B2:C3:D4:E5:F6"],
            "identity_keys": ["mqtt-slug:shelly-aa-bb", "network-host:198-51-100-40"],
            "classification_reasoning": "Local telemetry matched a grid meter profile.",
            "classification_confidence": 0.9,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Local HTTP telemetry is available.",
        next_step_hint="Keep the device monitorable through the local API.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _modbus_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-modbus",
        device_id="dev-modbus",
        asset_id="asset-modbus",
        asset_name="Grid Metering",
        display_name="Shelly Pro EM",
        manufacturer="Shelly",
        model="EM-Pro",
        firmware="1.0.0",
        device_type="grid_meter",
        discovery_sources=["modbus_live"],
        protocols=["modbus_tcp"],
        telemetry={"grid_power_kw": -2.4},
        evidence={
            "identity_keys": ["network-host:198-51-100-40"],
            "classification_reasoning": "SunSpec telemetry matched a grid meter profile.",
            "classification_confidence": 0.91,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Native SunSpec telemetry is available.",
        next_step_hint="Keep the device monitorable through Modbus TCP.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _mqtt_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-mqtt",
        device_id="dev-mqtt",
        asset_id="asset-mqtt",
        asset_name="Flexible Smart Load",
        display_name="Laundry Plug",
        manufacturer="Tasmota",
        model="MQTT energy device",
        firmware="unknown",
        device_type="smart_appliance",
        discovery_sources=["mqtt_live"],
        protocols=["mqtt"],
        telemetry={"power_w": 110},
        evidence={
            "mqtt_device_slug": "laundry-plug",
            "identity_keys": ["mqtt-slug:laundry-plug"],
            "classification_reasoning": "MQTT topics matched an appliance profile.",
            "classification_confidence": 0.86,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="MQTT telemetry is available.",
        next_step_hint="Keep the device monitorable through MQTT.",
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _broadcast_evcc_ipv4_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-broadcast-evcc-ipv4",
        device_id="dev-broadcast-evcc-ipv4",
        asset_id="asset-broadcast-evcc-ipv4",
        asset_name="EV Charging",
        display_name="evcc",
        manufacturer="evcc",
        model="upnp:rootdevice",
        firmware="unknown",
        device_type="wallbox",
        discovery_sources=["network_broadcast_live"],
        protocols=["mdns", "ssdp"],
        telemetry={},
        evidence={
            "identity_keys": ["network-host:198-51-100-158", "service-instance:evcc"],
            "classification_reasoning": "Network broadcast fingerprint matched evcc for the wallbox profile.",
            "classification_confidence": 0.87,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Broadcast evidence indicates an EV charging integration endpoint.",
        next_step_hint="Probe the local HTTP path for a telemetry adapter.",
        capabilities_hint={
            "visible": True,
            "monitorable": False,
            "controllable": False,
            "optimizable": False,
        },
    )


def _broadcast_evcc_ipv6_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-broadcast-evcc-ipv6",
        device_id="dev-broadcast-evcc-ipv6",
        asset_id="asset-broadcast-evcc-ipv6",
        asset_name="EV Charging",
        display_name="evcc._http._tcp.local",
        manufacturer="evcc",
        model="_http._tcp.local",
        firmware="unknown",
        device_type="wallbox",
        discovery_sources=["network_broadcast_live"],
        protocols=["mdns"],
        telemetry={},
        evidence={
            "identity_keys": [
                "network-host:fe80-921b-eff-fee4-d45f",
                "network-host:198-51-100-158",
                "service-instance:evcc",
            ],
            "classification_reasoning": "Network broadcast fingerprint matched evcc for the wallbox profile.",
            "classification_confidence": 0.87,
        },
        recovery_zone="auto_apply",
        issue_code=None,
        explanation_hint="Broadcast evidence indicates an EV charging integration endpoint.",
        next_step_hint="Probe the local HTTP path for a telemetry adapter.",
        capabilities_hint={
            "visible": True,
            "monitorable": False,
            "controllable": False,
            "optimizable": False,
        },
    )


def _eebus_evcc_candidate() -> RawCandidate:
    return RawCandidate(
        candidate_id="cand-eebus-evcc",
        device_id="dev-eebus-evcc",
        asset_id="asset-eebus-evcc",
        asset_name="EV Charging",
        display_name="MENNEKES CC612",
        manufacturer="MENNEKES",
        model="CC612_2S0R_CC",
        firmware="unknown",
        device_type="wallbox",
        discovery_sources=["eebus_ship_live"],
        protocols=["eebus_ship"],
        telemetry={"eebus_ship_advertised": True},
        evidence={
            "identity_keys": ["network-host:198-51-100-158", "eebus-ski:abc123"],
            "classification_reasoning": "EEBus SHIP advertisement matched evse for the wallbox profile.",
            "classification_confidence": 0.9,
        },
        recovery_zone="human_gated",
        issue_code=None,
        explanation_hint="Helios discovered an EEBus SHIP peer.",
        next_step_hint="Pair a trusted EEBus identity.",
        capabilities_hint={
            "visible": True,
            "monitorable": False,
            "controllable": False,
            "optimizable": False,
        },
    )


def test_reconcile_candidates_merges_shared_identity():
    reconciled = reconcile_candidates([_local_candidate(), _modbus_candidate()])

    assert len(reconciled) == 1
    assert reconciled[0].device_id == "dev-local"
    assert sorted(reconciled[0].discovery_sources) == ["local_network_live", "modbus_live"]
    assert sorted(reconciled[0].protocols) == ["http_local", "modbus_tcp"]
    assert reconciled[0].telemetry["phase_0_power_w"] == -810.1
    assert reconciled[0].telemetry["grid_power_kw"] == -2.4
    assert reconciled[0].evidence["reconciled"] is True


def test_reconcile_candidates_keeps_distinct_devices_separate():
    reconciled = reconcile_candidates([_local_candidate(), _mqtt_candidate()])

    assert len(reconciled) == 2
    assert {candidate.device_id for candidate in reconciled} == {"dev-local", "dev-mqtt"}


def test_reconcile_candidates_merges_broadcast_ipv4_and_ipv6_views():
    reconciled = reconcile_candidates([_broadcast_evcc_ipv4_candidate(), _broadcast_evcc_ipv6_candidate()])

    assert len(reconciled) == 1
    assert reconciled[0].device_id == "dev-broadcast-evcc-ipv4"
    assert "network-host:198-51-100-158" in reconciled[0].evidence["identity_keys"]


def test_reconcile_candidates_keeps_eebus_protocol_on_ship_peers():
    reconciled = reconcile_candidates([_broadcast_evcc_ipv4_candidate(), _eebus_evcc_candidate()])

    assert len(reconciled) == 1
    assert reconciled[0].device_id == "dev-eebus-evcc"
    assert sorted(reconciled[0].protocols) == ["eebus_ship", "mdns", "ssdp"]
    assert sorted(reconciled[0].discovery_sources) == ["eebus_ship_live", "network_broadcast_live"]
