from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from threading import Event
from typing import Any
from urllib.parse import urlsplit

from app.domain.enums import RecoveryZone
from app.services.discovery_blueprints import RawCandidate


MQTT_TOPIC_PATTERNS = [
    "tele/+/SENSOR",
    "tele/+/STATE",
    "stat/+/POWER",
    "shellies/+/emeter/+/power",
    "shellies/+/emeter/+/total",
    "shellies/+/relay/+/power",
    "shellies/+/relay/+/energy",
]


class MqttSourceError(RuntimeError):
    pass


@dataclass(slots=True)
class MqttMessage:
    topic: str
    payload: str
    retain: bool = False


@dataclass(slots=True)
class MqttDiscoveryBatch:
    source_name: str
    status: str
    message: str
    candidates: list[RawCandidate]


@dataclass(slots=True)
class MqttBrokerConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    tls: bool


def _slugify(value: str) -> str:
    normalized = [
        character.lower() if character.isalnum() else "-"
        for character in value
    ]
    slug = "".join(normalized).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "mqtt-device"


def _titleize_topic_name(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").title()


def _safe_json_loads(payload: str) -> dict[str, Any]:
    try:
        result = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def _parse_numeric(raw_value: Any) -> int | float | None:
    if isinstance(raw_value, (int, float)):
        return raw_value
    if raw_value is None:
        return None
    try:
        numeric = float(str(raw_value).strip())
    except ValueError:
        return None
    return int(numeric) if numeric.is_integer() else round(numeric, 3)


def _asset_name_for_type(device_type: str) -> str:
    mapping = {
        "grid_meter": "Grid Metering",
        "smart_appliance": "Flexible Smart Load",
        "unclassified_energy_device": "Unclassified Energy Device",
    }
    return mapping.get(device_type, "Flexible Smart Load")


def parse_mqtt_broker_url(broker_url: str) -> MqttBrokerConfig:
    parts = urlsplit(broker_url)
    if parts.scheme not in {"mqtt", "mqtts"}:
        raise MqttSourceError("MQTT broker URL must use mqtt or mqtts.")
    if not parts.hostname:
        raise MqttSourceError("MQTT broker URL is missing a hostname.")
    return MqttBrokerConfig(
        host=parts.hostname,
        port=parts.port or (8883 if parts.scheme == "mqtts" else 1883),
        username=parts.username,
        password=parts.password,
        tls=parts.scheme == "mqtts",
    )


def _classify_tasmota_device(device_slug: str) -> tuple[str, str, float]:
    lowered = device_slug.lower()
    if any(token in lowered for token in {"meter", "grid"}):
        return "grid_meter", "MQTT topic signature matched a Tasmota-style energy meter.", 0.82
    return "smart_appliance", "MQTT topic signature matched a Tasmota smart plug or appliance.", 0.86


def _build_tasmota_candidate(device_slug: str, messages: list[MqttMessage]) -> RawCandidate | None:
    sensor_message = next((message for message in messages if message.topic.endswith("/SENSOR")), None)
    if sensor_message is None:
        return None
    payload = _safe_json_loads(sensor_message.payload)
    energy = payload.get("ENERGY", {})
    if not isinstance(energy, dict):
        return None

    telemetry: dict[str, Any] = {}
    power = _parse_numeric(energy.get("Power"))
    if power is not None:
        telemetry["power_w"] = power
    today = _parse_numeric(energy.get("Today"))
    if today is not None:
        telemetry["energy_today_kwh"] = today
    voltage = _parse_numeric(energy.get("Voltage"))
    if voltage is not None:
        telemetry["voltage_v"] = voltage
    current = _parse_numeric(energy.get("Current"))
    if current is not None:
        telemetry["current_a"] = current
    if not telemetry:
        return None

    device_type, reasoning, confidence = _classify_tasmota_device(device_slug)
    matched_topics = sorted({message.topic for message in messages})
    return RawCandidate(
        candidate_id=f"cand-mqtt-{_slugify(device_slug)}",
        device_id=f"dev-mqtt-{_slugify(device_slug)}",
        asset_id=f"asset-mqtt-{_slugify(device_slug)}",
        asset_name=_asset_name_for_type(device_type),
        display_name=_titleize_topic_name(device_slug),
        manufacturer="Tasmota",
        model="MQTT energy device",
        firmware="unknown",
        device_type=device_type,
        discovery_sources=["mqtt_live"],
        protocols=["mqtt"],
        telemetry=telemetry,
        evidence={
            "mqtt_device_slug": device_slug,
            "identity_keys": [f"mqtt-slug:{_slugify(device_slug)}"],
            "mqtt_topics": matched_topics,
            "classification_reasoning": reasoning,
            "classification_confidence": confidence,
        },
        recovery_zone=RecoveryZone.AUTO_APPLY.value,
        issue_code=None,
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def _build_shelly_candidate(device_slug: str, messages: list[MqttMessage]) -> RawCandidate | None:
    telemetry: dict[str, Any] = {}
    device_type = "grid_meter" if "3em" in device_slug.lower() or any("/emeter/" in message.topic for message in messages) else "smart_appliance"
    for message in messages:
        parts = message.topic.split("/")
        value = _parse_numeric(message.payload)
        if value is None:
            continue
        if len(parts) >= 5 and parts[2] == "emeter":
            channel = parts[3]
            metric = parts[4]
            key = f"phase_{channel}_{metric}_w" if metric == "power" else f"phase_{channel}_{metric}"
            telemetry[key] = value
        elif len(parts) >= 5 and parts[2] == "relay":
            channel = parts[3]
            metric = parts[4]
            suffix = "w" if metric == "power" else metric
            telemetry[f"relay_{channel}_{metric}_{suffix}" if metric == "power" else f"relay_{channel}_{metric}"] = value
    if not telemetry:
        return None

    matched_topics = sorted({message.topic for message in messages})
    reasoning = (
        "MQTT topic signature matched a Shelly energy meter."
        if device_type == "grid_meter"
        else "MQTT topic signature matched a Shelly relay-based appliance."
    )
    confidence = 0.88 if device_type == "grid_meter" else 0.83
    return RawCandidate(
        candidate_id=f"cand-mqtt-{_slugify(device_slug)}",
        device_id=f"dev-mqtt-{_slugify(device_slug)}",
        asset_id=f"asset-mqtt-{_slugify(device_slug)}",
        asset_name=_asset_name_for_type(device_type),
        display_name=_titleize_topic_name(device_slug),
        manufacturer="Shelly",
        model="MQTT energy device",
        firmware="unknown",
        device_type=device_type,
        discovery_sources=["mqtt_live"],
        protocols=["mqtt"],
        telemetry=telemetry,
        evidence={
            "mqtt_device_slug": device_slug,
            "identity_keys": [f"mqtt-slug:{_slugify(device_slug)}"],
            "mqtt_topics": matched_topics,
            "classification_reasoning": reasoning,
            "classification_confidence": confidence,
        },
        recovery_zone=RecoveryZone.AUTO_APPLY.value,
        issue_code=None,
        capabilities_hint={
            "visible": True,
            "monitorable": True,
            "controllable": False,
            "optimizable": False,
        },
    )


def build_candidates_from_mqtt_messages(messages: list[MqttMessage]) -> list[RawCandidate]:
    grouped: dict[str, list[MqttMessage]] = {}

    for message in messages:
        topic = message.topic
        if topic.startswith("tele/"):
            parts = topic.split("/")
            if len(parts) >= 3:
                grouped.setdefault(f"tasmota:{parts[1]}", []).append(message)
        elif topic.startswith("stat/"):
            parts = topic.split("/")
            if len(parts) >= 3:
                grouped.setdefault(f"tasmota:{parts[1]}", []).append(message)
        elif topic.startswith("shellies/"):
            parts = topic.split("/")
            if len(parts) >= 2:
                grouped.setdefault(f"shelly:{parts[1]}", []).append(message)

    candidates: list[RawCandidate] = []
    for group_key, topic_messages in grouped.items():
        namespace, device_slug = group_key.split(":", 1)
        candidate = (
            _build_tasmota_candidate(device_slug, topic_messages)
            if namespace == "tasmota"
            else _build_shelly_candidate(device_slug, topic_messages)
        )
        if candidate is not None:
            candidates.append(candidate)
    return sorted(candidates, key=lambda candidate: candidate.display_name.lower())


class MqttMessageCollector:
    def __init__(
        self,
        broker_url: str,
        connect_timeout_seconds: float = 5.0,
        probe_window_seconds: float = 2.0,
    ) -> None:
        self.broker = parse_mqtt_broker_url(broker_url)
        self.connect_timeout_seconds = connect_timeout_seconds
        self.probe_window_seconds = probe_window_seconds

    def collect_messages(self) -> list[MqttMessage]:
        try:
            import paho.mqtt.client as mqtt_client
        except ModuleNotFoundError as exc:
            raise MqttSourceError("The 'paho-mqtt' package is required for MQTT live discovery.") from exc

        callback_api = getattr(mqtt_client, "CallbackAPIVersion", None)
        client = (
            mqtt_client.Client(callback_api.VERSION2)
            if callback_api is not None
            else mqtt_client.Client()
        )

        connected = Event()
        messages: list[MqttMessage] = []
        errors: list[str] = []

        def on_connect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
            code = getattr(reason_code, "value", reason_code)
            if code != 0:
                errors.append(f"MQTT broker rejected the connection with code {code}.")
                connected.set()
                return
            for topic in MQTT_TOPIC_PATTERNS:
                client.subscribe(topic)
            connected.set()

        def on_message(client: Any, userdata: Any, message: Any) -> None:
            payload = message.payload.decode("utf-8", errors="ignore")
            messages.append(MqttMessage(topic=message.topic, payload=payload, retain=bool(message.retain)))

        client.on_connect = on_connect
        client.on_message = on_message

        if self.broker.username:
            client.username_pw_set(self.broker.username, self.broker.password)
        if self.broker.tls:
            client.tls_set()

        try:
            client.connect(self.broker.host, self.broker.port, keepalive=max(int(self.connect_timeout_seconds), 5))
        except (OSError, socket.error) as exc:
            raise MqttSourceError(f"Unable to connect to the MQTT broker: {exc}.") from exc

        client.loop_start()
        try:
            if not connected.wait(timeout=self.connect_timeout_seconds):
                raise MqttSourceError("Timed out while connecting to the MQTT broker.")
            if errors:
                raise MqttSourceError(errors[0])
            time.sleep(self.probe_window_seconds)
        finally:
            try:
                client.disconnect()
            finally:
                client.loop_stop()

        return messages


def discover_mqtt_site(
    broker_url: str,
    connect_timeout_seconds: float = 5.0,
    probe_window_seconds: float = 2.0,
) -> MqttDiscoveryBatch:
    try:
        messages = MqttMessageCollector(
            broker_url=broker_url,
            connect_timeout_seconds=connect_timeout_seconds,
            probe_window_seconds=probe_window_seconds,
        ).collect_messages()
    except MqttSourceError as exc:
        return MqttDiscoveryBatch(
            source_name="mqtt_live",
            status="failed",
            message=str(exc),
            candidates=[],
        )

    candidates = build_candidates_from_mqtt_messages(messages)
    if not candidates:
        return MqttDiscoveryBatch(
            source_name="mqtt_live",
            status="completed",
            message="MQTT live discovery completed, but no known energy-topic signatures were found.",
            candidates=[],
        )

    return MqttDiscoveryBatch(
        source_name="mqtt_live",
        status="completed",
        message=f"Imported {len(candidates)} energy-relevant MQTT device candidates.",
        candidates=candidates,
    )
