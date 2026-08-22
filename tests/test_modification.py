"""
Tests for the On-the-Fly Modification Engine.
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from core.modification import (
    InterceptedMessage,
    ModificationAction,
    ModificationEngine,
    ModificationOperation,
    ModificationRule,
)
from adapters.base import InterceptedRequest, ProtocolType


# ---------------------------------------------------------------------------
#  Mock config helpers
# ---------------------------------------------------------------------------

class MockConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockConfigManager:
    """Mock config manager that avoids YAML loading and hot-reload."""
    def __init__(self):
        self.config = MockConfig()
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def add_change_callback(self, callback):
        """ModificationEngine calls this in __init__."""
        self.callbacks.append(callback)


def make_engine():
    """Create ModificationEngine with mocked config manager."""
    cm = MockConfigManager()
    return ModificationEngine(config_manager=cm)


def make_intercepted_request(**overrides) -> InterceptedRequest:
    """Build an InterceptedRequest with defaults."""
    defaults = dict(
        device_id="device-001",
        timestamp=datetime.now(UTC),
        protocol=ProtocolType.HTTP,
        method="POST",
        path="/v1/command",
        headers={"content-type": "application/json"},
        body={"temp": 25, "mode": "cool"},
    )
    defaults.update(overrides)
    # Separate non-field kwargs
    vendor = defaults.pop("vendor", "shelly")
    metadata = defaults.pop("metadata", {"device_type": "ac", "intent": "set_temperature"})
    req = InterceptedRequest(**defaults)
    setattr(req, "vendor", vendor)
    setattr(req, "metadata", metadata)
    return req


def make_msg(**overrides) -> InterceptedMessage:
    """Helper to create InterceptedMessage with defaults."""
    defaults = {
        "direction": "request",
        "device_id": "test-device",
        "vendor": "test",
        "device_type": "ac",
        "intent": "set_temperature",
        "method": "POST",
        "path": "/v1/command",
        "headers": {"content-type": "application/json"},
        "body": {"temp": 25, "mode": "cool", "fan_speed": "high"},
        "query_params": {},
        "metadata": {},
    }
    defaults.update(overrides)
    return InterceptedMessage(**defaults)


# ---------------------------------------------------------------------------
#  ModificationRule — matching
# ---------------------------------------------------------------------------

class TestModificationRule:
    """ModificationRule matching and apply()."""

    def test_rule_construction(self):
        rule = ModificationRule(
            name="test-rule",
            match_vendor="shelly",
            action=ModificationAction.MODIFY,
            action_params={"operation": "set", "value": 30, "field_path": "temp"},
        )
        assert rule.name == "test-rule"
        assert rule.match_vendor == "shelly"
        assert rule.action == ModificationAction.MODIFY
        assert rule.priority == 10
        assert rule.enabled is True

    def test_vendor_match(self):
        rule = ModificationRule(name="r1", match_vendor="shelly", action=ModificationAction.MODIFY)
        msg = make_msg(vendor="shelly")
        assert rule.matches(msg, "request")
        msg.vendor = "other"
        assert not rule.matches(msg, "request")

    def test_device_type_match(self):
        rule = ModificationRule(name="r1", match_device_type="ac", action=ModificationAction.MODIFY)
        assert rule.matches(make_msg(device_type="ac"), "request")
        assert not rule.matches(make_msg(device_type="heat_pump"), "request")

    def test_intent_match(self):
        rule = ModificationRule(name="r1", match_intent="set_temperature", action=ModificationAction.MODIFY)
        assert rule.matches(make_msg(intent="set_temperature"), "request")
        assert not rule.matches(make_msg(intent="turn_off"), "request")

    def test_method_match(self):
        rule = ModificationRule(name="r1", match_method="POST", action=ModificationAction.MODIFY)
        assert rule.matches(make_msg(method="POST"), "request")
        assert not rule.matches(make_msg(method="GET"), "request")

    def test_path_pattern_regex(self):
        rule = ModificationRule(name="r1", match_path_pattern=r"/v1/command", action=ModificationAction.MODIFY)
        assert rule.matches(make_msg(path="/v1/command"), "request")
        assert not rule.matches(make_msg(path="/v2/other"), "request")

    def test_headers_match(self):
        rule = ModificationRule(
            name="r1", match_headers={"content-type": "application/json"},
            action=ModificationAction.MODIFY,
        )
        assert rule.matches(make_msg(headers={"content-type": "application/json"}), "request")
        assert not rule.matches(make_msg(headers={"content-type": "text/xml"}), "request")

    def test_direction_filter(self):
        rule = ModificationRule(name="r1", direction="request", action=ModificationAction.MODIFY)
        assert rule.matches(make_msg(direction="request"), "request")
        assert not rule.matches(make_msg(direction="response"), "response")

    def test_disabled_rule_no_match(self):
        rule = ModificationRule(
            name="r1", match_vendor="shelly", action=ModificationAction.MODIFY, enabled=False
        )
        assert not rule.matches(make_msg(vendor="shelly"), "request")

    def test_field_path_match_with_value(self):
        body = {"temperature": {"current": 25, "target": 22}}
        rule = ModificationRule(
            name="r1",
            match_field_path="temperature.current",
            match_value=25,
            action=ModificationAction.MODIFY,
            action_params={"operation": "set", "value": 26},
        )
        msg = make_msg(body=body)
        assert rule.matches(msg, "request")
        msg.body = {"temperature": {"current": 30}}
        assert not rule.matches(msg, "request")


# ---------------------------------------------------------------------------
#  ModificationRule — applying actions
# ---------------------------------------------------------------------------

class TestModificationApply:
    """ModificationRule.apply() for each action."""

    def test_modify_set_action(self):
        body = {"temp": 25, "mode": "cool"}
        rule = ModificationRule(
            name="set-temp",
            match_field_path="temp",
            action=ModificationAction.MODIFY,
            action_params={"operation": "set", "value": 30},
        )
        msg = make_msg(body=body)
        result = rule.apply(msg)
        assert result.body["temp"] == 30
        assert result.body["mode"] == "cool"  # unchanged

    def test_modify_add_action(self):
        body = {"count": 10}
        rule = ModificationRule(
            name="add",
            match_field_path="count",
            action=ModificationAction.MODIFY,
            action_params={"operation": "add", "amount": 5},
        )
        msg = make_msg(body=body)
        result = rule.apply(msg)
        assert result.body["count"] == 15

    def test_modify_multiply_action(self):
        body = {"power": 100}
        rule = ModificationRule(
            name="multiply",
            match_field_path="power",
            action=ModificationAction.MODIFY,
            action_params={"operation": "multiply", "factor": 0.5},
        )
        msg = make_msg(body=body)
        result = rule.apply(msg)
        assert result.body["power"] == 50.0

    def test_modify_clamp_action(self):
        body = {"value": 150}
        rule = ModificationRule(
            name="clamp",
            match_field_path="value",
            action=ModificationAction.MODIFY,
            action_params={"operation": "clamp", "min": 0, "max": 100},
        )
        msg = make_msg(body=body)
        result = rule.apply(msg)
        assert result.body["value"] == 100

        body2 = {"value": -10}
        msg2 = make_msg(body=body2)
        result2 = rule.apply(msg2)
        assert result2.body["value"] == 0

    def test_modify_replace_string_action(self):
        body = {"name": "device-room1"}
        rule = ModificationRule(
            name="replace-str",
            match_field_path="name",
            action=ModificationAction.MODIFY,
            action_params={"operation": "replace", "old": "room1", "new": "living"},
        )
        msg = make_msg(body=body)
        result = rule.apply(msg)
        assert result.body["name"] == "device-living"

    def test_modify_remove_action(self):
        body = {"temp": 25, "secret": "hidden"}
        rule = ModificationRule(
            name="remove",
            match_field_path="secret",
            action=ModificationAction.MODIFY,
            action_params={"operation": "remove"},
        )
        msg = make_msg(body=body)
        result = rule.apply(msg)
        assert result.body["secret"] is None

    def test_block_action(self):
        rule = ModificationRule(name="block-all", action=ModificationAction.BLOCK)
        msg = make_msg()
        result = rule.apply(msg)
        assert result.blocked is True
        assert "Rule: block-all" in result.block_reason

    def test_inject_action(self):
        rule = ModificationRule(
            name="inject-field",
            action=ModificationAction.INJECT,
            action_params={"field_path": "source", "value": "edge_ai"},
        )
        msg = make_msg(body={"temp": 25})
        result = rule.apply(msg)
        assert result.body["source"] == "edge_ai"
        assert result.body["temp"] == 25

    def test_replace_action(self):
        new_body = {"replaced": True}
        rule = ModificationRule(
            name="replace-body",
            action=ModificationAction.REPLACE,
            action_params={"body": new_body},
        )
        msg = make_msg(body={"old": "data"})
        result = rule.apply(msg)
        assert result.body == new_body

    def test_redirect_action(self):
        rule = ModificationRule(
            name="redirect",
            action=ModificationAction.REDIRECT,
            action_params={"path": "/new/endpoint", "host": "local.proxy"},
        )
        msg = make_msg(path="/old/path")
        result = rule.apply(msg)
        assert result.path == "/new/endpoint"
        assert result.headers["host"] == "local.proxy"

    def test_delay_action(self):
        rule = ModificationRule(
            name="delay",
            action=ModificationAction.DELAY,
            action_params={"delay_ms": 500},
        )
        msg = make_msg()
        result = rule.apply(msg)
        assert result.metadata.get("artificial_delay_ms") == 500

    def test_modifications_tracking(self):
        rule = ModificationRule(
            name="track-me",
            match_field_path="temp",
            action=ModificationAction.MODIFY,
            action_params={"operation": "set", "value": 30},
        )
        msg = make_msg(body={"temp": 25})
        result = rule.apply(msg)
        assert len(result.modifications) == 1
        assert result.modifications[0]["rule"] == "track-me"
        assert result.modifications[0]["action"] == "modify"

    # ------------------------------------------------------------------
    #  JSON path helpers (tested through apply / instance method calls)
    # ------------------------------------------------------------------

    def test_get_json_path_nested(self):
        rule = ModificationRule(name="g1", match_field_path="a.b.c", action=ModificationAction.MODIFY)
        body = {"a": {"b": {"c": 42}}}
        val = rule._get_json_path(body, "a.b.c")
        assert val == 42

    def test_get_json_path_array(self):
        rule = ModificationRule(name="g2", action=ModificationAction.MODIFY)
        body = {"items": [{"id": 1}, {"id": 2}]}
        val = rule._get_json_path(body, "items[0].id")
        assert val == 1

    def test_get_json_path_dollar_prefix(self):
        rule = ModificationRule(name="g3", action=ModificationAction.MODIFY)
        body = {"x": 10}
        val = rule._get_json_path(body, "$.x")
        assert val == 10

    def test_get_json_path_missing(self):
        rule = ModificationRule(name="g4", action=ModificationAction.MODIFY)
        body = {"a": 1}
        val = rule._get_json_path(body, "b")
        assert val is None

    def test_set_json_path_nested(self):
        body = {"a": {"b": 1}}
        msg = make_msg(body=body)
        rule = ModificationRule(
            name="s1",
            match_field_path="a.b",
            action=ModificationAction.MODIFY,
            action_params={"operation": "set", "value": 99},
        )
        result = rule.apply(msg)
        assert result.body["a"]["b"] == 99


# ---------------------------------------------------------------------------
#  ModificationEngine
# ---------------------------------------------------------------------------

class TestModificationEngine:
    def _default_mod_config(self):
        """Return a config mock with modification sub-config present."""
        mod_cfg = MockConfig(
            enabled=True,
            rules=[],
            audit_log_enabled=False,
        )
        return MockConfig(modification=mod_cfg)

    def test_no_rules(self):
        engine = make_engine()
        req = make_intercepted_request(vendor="test-vendor")
        modified, was_modified = engine.process_request(req)
        assert modified is not None
        assert not was_modified

    def test_single_match(self):
        engine = make_engine()
        rule = ModificationRule(
            name="r1", match_vendor="shelly",
            action=ModificationAction.INJECT,
            action_params={"field_path": "source", "value": "edge"},
        )
        engine.add_rule(rule)
        req = make_intercepted_request(vendor="shelly")
        modified, was_modified = engine.process_request(req)
        assert was_modified
        assert modified.body.get("source") == "edge"

    def test_no_match(self):
        engine = make_engine()
        rule = ModificationRule(
            name="r1", match_vendor="shelly",
            action=ModificationAction.BLOCK,
        )
        engine.add_rule(rule)
        req = make_intercepted_request(vendor="tasmota")
        modified, was_modified = engine.process_request(req)
        assert not was_modified
            # No block attribute set on InterceptedRequest when no matches occur

    def test_priority_ordering(self):
        engine = make_engine()
        low = ModificationRule(
            "low", match_vendor="shelly",
            action=ModificationAction.INJECT,
            action_params={"field_path": "source", "value": "low"},
            priority=5,
        )
        high = ModificationRule(
            "high", match_vendor="shelly",
            action=ModificationAction.INJECT,
            action_params={"field_path": "source", "value": "high"},
            priority=20,
        )
        engine.add_rule(low)
        engine.add_rule(high)
        req = make_intercepted_request(vendor="shelly")
        modified, was_modified = engine.process_request(req)
        assert was_modified
        # Last-applied rule wins (low runs after high)
        assert modified.body.get("source") == "low"

    def test_get_rules(self):
        engine = make_engine()
        assert isinstance(engine.get_rules(), list)
        assert len(engine.get_rules()) == 0

    def test_add_and_remove_rule(self):
        engine = make_engine()
        rule = ModificationRule("to-remove", match_vendor="test", action=ModificationAction.BLOCK)
        engine.add_rule(rule)
        assert len(engine.get_rules()) == 1
        assert engine.remove_rule("to-remove") is True
        assert len(engine.get_rules()) == 0
        assert engine.remove_rule("nonexistent") is False

    def test_get_audit_log(self):
        engine = make_engine()
        rule = ModificationRule("r1", match_vendor="shelly", action=ModificationAction.BLOCK)
        engine.add_rule(rule)
        req = make_intercepted_request(vendor="shelly")
        engine.process_request(req)
        audit = engine.get_audit_log()
        assert len(audit) > 0
        assert audit[0]["rule"] == "r1"