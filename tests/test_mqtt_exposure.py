"""The MQTT broker says how it is exposed (S-5)."""

from __future__ import annotations

from core.config import MQTTServerConfig
from core.protocol_servers.mqtt_server import exposure_warnings


def test_default_broker_warns_it_is_open_to_every_interface():
    (warning,) = exposure_warnings(MQTTServerConfig())
    assert "all interfaces" in warning
    assert "1883" in warning


def test_a_bound_interface_is_not_flagged():
    assert exposure_warnings(MQTTServerConfig(host="192.168.1.10")) == []


def test_tls_setting_that_does_nothing_is_reported():
    warnings = exposure_warnings(MQTTServerConfig(host="192.168.1.10", tls_enabled=True))
    assert len(warnings) == 1
    assert "tls_enabled is not supported" in warnings[0]
