from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Helios Home API"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./helios_home.db"
    local_scan_timeout_seconds: float = 1.5
    local_scan_concurrency: int = 32
    local_scan_max_hosts: int = 256
    broadcast_timeout_seconds: float = 1.0
    broadcast_max_service_types: int = 12
    modbus_live_enabled: bool = False
    modbus_timeout_seconds: float = 1.0
    modbus_concurrency: int = 32
    modbus_max_hosts: int = 256
    mqtt_timeout_seconds: float = 5.0
    mqtt_probe_window_seconds: float = 2.0
    eebus_interface_ip: str = ""
    eebus_timeout_seconds: float = 3.0
    eebus_tls_check_enabled: bool = False
    eebus_ship_bind_host: str = "0.0.0.0"
    eebus_ship_port: int = 4712
    eebus_ship_path: str = "/ship/"
    eebus_ship_device_id: str = "HELIOS-HOME-HEMS"
    native_writes_enabled: bool = False
    write_http_timeout_seconds: float = 3.0
    agent_provider: str = "stub"
    agent_config_path: str = "~/.config/helios-home/agent-provider.json"
    agent_stream_delay_ms: int = 12
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HELIOS_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
