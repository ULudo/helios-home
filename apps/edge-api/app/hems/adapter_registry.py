from __future__ import annotations

SIMULATION_ADAPTER_NAME = "telemetry_simulation"

SUPPORTED_NATIVE_WRITE_PROFILES = frozenset(
    {
        "shelly_http_relay",
        "tasmota_http_power",
        "sunspec_storage_basic_rate",
        "sunspec_der_wmax_pct",
        "sunspec_immediate_wmax_pct",
    }
)


def is_supported_native_dispatch_profile(profile_name: str) -> bool:
    return profile_name in SUPPORTED_NATIVE_WRITE_PROFILES
