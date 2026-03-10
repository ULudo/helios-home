from app.services.mqtt import MqttMessage, build_candidates_from_mqtt_messages, parse_mqtt_broker_url


def test_parse_mqtt_broker_url_supports_auth_and_tls():
    broker = parse_mqtt_broker_url("mqtts://user:pass@mqtt.example:8883")

    assert broker.host == "mqtt.example"
    assert broker.port == 8883
    assert broker.username == "user"
    assert broker.password == "pass"
    assert broker.tls is True


def test_build_candidates_from_tasmota_topics():
    candidates = build_candidates_from_mqtt_messages(
        [
            MqttMessage(
                topic="tele/laundry-plug/SENSOR",
                payload='{"ENERGY":{"Power":112,"Today":0.8,"Voltage":231}}',
                retain=True,
            ),
            MqttMessage(topic="stat/laundry-plug/POWER", payload="ON", retain=True),
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].manufacturer == "Tasmota"
    assert candidates[0].device_type == "smart_appliance"
    assert candidates[0].telemetry["power_w"] == 112
    assert candidates[0].telemetry["energy_today_kwh"] == 0.8


def test_build_candidates_from_shelly_topics():
    candidates = build_candidates_from_mqtt_messages(
        [
            MqttMessage(topic="shellies/shelly3em-98ABC/emeter/0/power", payload="-820.4", retain=True),
            MqttMessage(topic="shellies/shelly3em-98ABC/emeter/1/power", payload="-790.1", retain=True),
            MqttMessage(topic="shellies/shelly3em-98ABC/emeter/2/power", payload="-801.7", retain=True),
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].manufacturer == "Shelly"
    assert candidates[0].device_type == "grid_meter"
    assert candidates[0].telemetry["phase_0_power_w"] == -820.4
