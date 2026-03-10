from app.services.local_network import (
    HttpDeviceContext,
    HttpDocument,
    build_candidate_from_http_context,
)


def test_build_candidate_from_tasmota_http_context():
    candidate = build_candidate_from_http_context(
        HttpDeviceContext(
            host="198.51.100.30",
            base_url="http://198.51.100.30",
            root=HttpDocument(
                path="/",
                status_code=200,
                headers={"server": "Tasmota/15.1.0 (ESP8266EX)"},
                text="<html><title>Tasmota Main Menu</title></html>",
                json_body=None,
            ),
            documents={
                "/cm?cmnd=Status%200": HttpDocument(
                    path="/cm?cmnd=Status%200",
                    status_code=200,
                    headers={"content-type": "application/json"},
                    text="{}",
                    json_body={
                        "Status": {"FriendlyName": ["Laundry Plug"]},
                        "StatusFWR": {"Version": "15.1.0"},
                        "StatusNET": {"Hostname": "laundry-plug", "Mac": "AA:BB:CC:DD:EE:FF"},
                        "StatusSNS": {"ENERGY": {"Power": 112, "Today": 0.8, "Voltage": 231}},
                    },
                )
            },
        )
    )

    assert candidate is not None
    assert candidate.manufacturer == "Tasmota"
    assert candidate.device_type == "smart_appliance"
    assert candidate.telemetry["power_w"] == 112
    assert candidate.capabilities_hint["monitorable"] is True


def test_build_candidate_from_shelly_http_context():
    candidate = build_candidate_from_http_context(
        HttpDeviceContext(
            host="198.51.100.40",
            base_url="http://198.51.100.40",
            root=HttpDocument(
                path="/",
                status_code=200,
                headers={"server": "ShellyHTTP/1.0.0"},
                text="<html><title>Shelly 3EM</title></html>",
                json_body=None,
            ),
            documents={
                "/shelly": HttpDocument(
                    path="/shelly",
                    status_code=200,
                    headers={"content-type": "application/json"},
                    text="{}",
                    json_body={"type": "SHEM-3", "mac": "A1:B2:C3:D4:E5:F6"},
                ),
                "/status": HttpDocument(
                    path="/status",
                    status_code=200,
                    headers={"content-type": "application/json"},
                    text="{}",
                    json_body={
                        "mac": "A1:B2:C3:D4:E5:F6",
                        "emeters": [
                            {"power": -820.4, "total": 1234},
                            {"power": -790.1, "total": 1221},
                            {"power": -801.7, "total": 1204},
                        ],
                    },
                ),
            },
        )
    )

    assert candidate is not None
    assert candidate.manufacturer == "Shelly"
    assert candidate.device_type == "grid_meter"
    assert candidate.telemetry["phase_0_power_w"] == -820.4
    assert candidate.capabilities_hint["monitorable"] is True


def test_build_candidate_from_generic_energy_http_context_is_visible_only():
    candidate = build_candidate_from_http_context(
        HttpDeviceContext(
            host="198.51.100.90",
            base_url="http://198.51.100.90",
            root=HttpDocument(
                path="/",
                status_code=200,
                headers={"server": "nginx"},
                text="<html><title>Solar Inverter Gateway</title><body>PV inverter status portal</body></html>",
                json_body=None,
            ),
            documents={},
        )
    )

    assert candidate is not None
    assert candidate.device_type == "pv_inverter"
    assert candidate.capabilities_hint["monitorable"] is False
    assert candidate.protocols == ["http_local"]


def test_non_energy_http_context_is_ignored():
    candidate = build_candidate_from_http_context(
        HttpDeviceContext(
            host="198.51.100.10",
            base_url="http://198.51.100.10",
            root=HttpDocument(
                path="/",
                status_code=200,
                headers={"server": "nginx"},
                text="<html><title>Media Server</title><body>Movies and TV</body></html>",
                json_body=None,
            ),
            documents={},
        )
    )

    assert candidate is None
