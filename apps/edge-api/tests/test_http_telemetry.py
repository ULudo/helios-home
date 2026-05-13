from __future__ import annotations

import httpx

from app.services.http_telemetry import probe_http_endpoint_telemetry


def test_probe_http_endpoint_telemetry_decodes_shelly_rpc_sample():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rpc/Shelly.GetStatus":
            return httpx.Response(
                200,
                json={
                    "switch:0": {
                        "apower": 17.4,
                        "current": 0.08,
                        "voltage": 239.2,
                        "aenergy": {"total": 12345.6},
                    }
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe_http_endpoint_telemetry(
            base_url="http://shelly.local",
            profiles=["shelly"],
            timeout_seconds=0.2,
            client=client,
        )

    assert result.status == "updated"
    assert result.source == "shelly_http"
    assert result.telemetry == {
        "switch_0_power_w": 17.4,
        "switch_0_current_a": 0.08,
        "switch_0_voltage_v": 239.2,
        "switch_0_energy_total": 12345.6,
    }


def test_probe_http_endpoint_telemetry_decodes_tasmota_status_sample():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cm":
            return httpx.Response(
                200,
                json={
                    "StatusSNS": {
                        "ENERGY": {
                            "Current": 0.034,
                            "Power": 1,
                            "ReactivePower": 8,
                            "Today": 0.02,
                            "Total": 40.32,
                            "Voltage": 238,
                        }
                    }
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe_http_endpoint_telemetry(
            base_url="http://tasmota.local",
            profiles=["tasmota"],
            timeout_seconds=0.2,
            client=client,
        )

    assert result.status == "updated"
    assert result.source == "tasmota_http"
    assert result.telemetry == {
        "current_a": 0.034,
        "power_w": 1,
        "reactive_power_var": 8,
        "energy_today_kwh": 0.02,
        "energy_total_kwh": 40.32,
        "voltage_v": 238,
    }
