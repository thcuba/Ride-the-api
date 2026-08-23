"""
Tests for Traffic Selection Engine.
"""

import ipaddress

from core.traffic_selector import (
    MatchType,
    TrafficAction,
    TrafficRequestInfo,
    TrafficRule,
    TrafficScope,
    TrafficSelector,
    create_request_info,
)


class TestTrafficRule:
    """TrafficRule construction and matching."""

    def test_cidr_rule_construction(self):
        rule = TrafficRule(
            name="local-net",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.CIDR,
            match_value="192.168.1.0/24",
            action=TrafficAction.INTERCEPT,
            priority=20,
        )
        assert rule.name == "local-net"
        assert rule.scope == TrafficScope.LOCAL
        assert rule.match_type == MatchType.CIDR
        assert rule.match_value == "192.168.1.0/24"
        assert rule.action == TrafficAction.INTERCEPT
        assert rule.priority == 20  # noqa: PLR2004
        assert rule.enabled is True
        assert rule._cidr_network == ipaddress.ip_network("192.168.1.0/24")

    def test_hostname_rule_compiles_regex(self):
        rule = TrafficRule(
            name="shelly-cloud",
            scope=TrafficScope.EXTERNAL,
            match_type=MatchType.HOSTNAME,
            match_value="*.shelly.cloud",
            action=TrafficAction.PASSTHROUGH,
        )
        assert rule._compiled_pattern is not None
        assert rule._compiled_pattern.match("device.shelly.cloud")
        assert rule._compiled_pattern.match("api.shelly.cloud")
        assert not rule._compiled_pattern.match("shelly.com")
        assert not rule._compiled_pattern.match("evil.shelly.cloud.phishing.com")

    def test_vendor_rule(self):
        rule = TrafficRule(
            name="shelly-vendor",
            scope=TrafficScope.EXTERNAL,
            match_type=MatchType.VENDOR,
            match_value="shelly",
            action=TrafficAction.INTERCEPT,
        )
        assert rule.matches(
            TrafficRequestInfo(client_ip="1.2.3.4", vendor="shelly", is_local=False)
        )
        assert not rule.matches(
            TrafficRequestInfo(client_ip="1.2.3.4", vendor="tasmota", is_local=False)
        )

    def test_device_id_rule(self):
        rule = TrafficRule(
            name="specific-device",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.DEVICE_ID,
            match_value="shelly-device-001",
            action=TrafficAction.INTERCEPT,
        )
        info = TrafficRequestInfo(
            client_ip="192.168.1.100", device_id="shelly-device-001", is_local=True
        )
        assert rule.matches(info)
        info.device_id = "other-device"
        assert not rule.matches(info)

    def test_disabled_rule_never_matches(self):
        rule = TrafficRule(
            name="disabled",
            scope=TrafficScope.EXTERNAL,
            match_type=MatchType.HOSTNAME,
            match_value="*.cloud",
            action=TrafficAction.INTERCEPT,
            enabled=False,
        )
        info = TrafficRequestInfo(client_ip="1.2.3.4", hostname="api.cloud.com", is_local=False)
        assert not rule.matches(info)

    def test_scope_must_match_local(self):
        rule = TrafficRule(
            name="local-only",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.CIDR,
            match_value="10.0.0.0/8",
            action=TrafficAction.INTERCEPT,
        )
        local_info = TrafficRequestInfo(client_ip="10.0.0.5", is_local=True)
        external_info = TrafficRequestInfo(client_ip="8.8.8.8", is_local=False)
        assert rule.matches(local_info)
        assert not rule.matches(external_info)

    def test_cidr_matching_valid(self):
        rule = TrafficRule(
            name="cidr-test",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.CIDR,
            match_value="10.0.0.0/8",
            action=TrafficAction.INTERCEPT,
        )
        assert rule.matches(TrafficRequestInfo("10.0.0.1", is_local=True))
        assert rule.matches(TrafficRequestInfo("10.255.255.255", is_local=True))
        assert not rule.matches(TrafficRequestInfo("11.0.0.1", is_local=True))

    def test_cidr_matching_invalid_ip_returns_false(self):
        rule = TrafficRule(
            name="cidr-test",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.CIDR,
            match_value="10.0.0.0/8",
            action=TrafficAction.INTERCEPT,
        )
        info = TrafficRequestInfo(client_ip="not-an-ip", is_local=True)
        assert not rule.matches(info)


class TestTrafficSelector:
    """TrafficSelector CRUD and evaluate."""

    def test_empty_selector_defaults_to_intercept(self):
        selector = TrafficSelector()
        assert selector.default_action == TrafficAction.INTERCEPT
        info = TrafficRequestInfo(
            client_ip="8.8.8.8", is_local=False, hostname="unknown.device.com"
        )
        assert selector.evaluate(info) == TrafficAction.INTERCEPT

    def test_add_and_remove_rule(self):
        selector = TrafficSelector()
        initial_count = len(selector.get_rules())
        rule = TrafficRule(
            name="test-rule",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.CIDR,
            match_value="192.168.1.0/24",
            action=TrafficAction.PASSTHROUGH,
        )
        selector.add_rule(rule)
        assert len(selector.get_rules()) == initial_count + 1

        removed = selector.remove_rule("test-rule")
        assert removed is True
        assert len(selector.get_rules()) == initial_count
        assert selector.remove_rule("nonexistent") is False

    def test_update_rule(self):
        selector = TrafficSelector()
        rule = TrafficRule(
            name="updatable",
            scope=TrafficScope.LOCAL,
            match_type=MatchType.CIDR,
            match_value="10.0.0.0/8",
            action=TrafficAction.INTERCEPT,
        )
        selector.add_rule(rule)
        selector.update_rule("updatable", action=TrafficAction.PASSTHROUGH, priority=5)
        rules = selector.get_rules()
        updated = [r for r in rules if r.name == "updatable"][0]
        assert updated.action == TrafficAction.PASSTHROUGH
        assert updated.priority == 5  # noqa: PLR2004
        assert selector.update_rule("nonexistent", priority=1) is False

    def test_update_rule_recompiles_hostname_pattern(self):
        """Editing match_value must recompile the cached regex (not stay stale)."""
        selector = TrafficSelector()
        rule = TrafficRule(
            name="host",
            scope=TrafficScope.EXTERNAL,
            match_type=MatchType.HOSTNAME,
            match_value="*.example.com",
            action=TrafficAction.INTERCEPT,
        )
        selector.add_rule(rule)
        old = TrafficRequestInfo(client_ip="8.8.8.8", hostname="www.example.com")
        assert rule.matches(old) is True

        # Change the match pattern; the compiled regex must follow.
        selector.update_rule("host", match_value="*.other.com")
        new = TrafficRequestInfo(client_ip="8.8.8.8", hostname="www.other.com")
        old2 = TrafficRequestInfo(client_ip="8.8.8.8", hostname="www.example.com")
        assert rule.matches(new) is True
        assert rule.matches(old2) is False

    def test_priority_ordering(self):
        selector = TrafficSelector()
        low = TrafficRule(
            "low",
            TrafficScope.LOCAL,
            MatchType.CIDR,
            "192.168.0.0/16",
            TrafficAction.PASSTHROUGH,
            priority=5,
        )
        high = TrafficRule(
            "high",
            TrafficScope.LOCAL,
            MatchType.CIDR,
            "192.168.1.0/24",
            TrafficAction.INTERCEPT,
            priority=25,
        )
        selector.add_rule(low)
        selector.add_rule(high)
        rules = selector.get_rules()
        assert rules[0].priority >= rules[1].priority

    def test_first_match_wins(self):
        selector = TrafficSelector()
        block = TrafficRule(
            "block-all",
            TrafficScope.LOCAL,
            MatchType.CIDR,
            "0.0.0.0/0",
            TrafficAction.PASSTHROUGH,
            priority=1,
        )
        intercept = TrafficRule(
            "intercept-some",
            TrafficScope.LOCAL,
            MatchType.CIDR,
            "192.168.0.0/16",
            TrafficAction.INTERCEPT,
            priority=10,
        )
        selector.add_rule(block)
        selector.add_rule(intercept)
        info = TrafficRequestInfo(client_ip="192.168.1.10", is_local=True)
        assert selector.evaluate(info) == TrafficAction.INTERCEPT

    def test_get_rules_returns_copy(self):
        selector = TrafficSelector()
        initial_count = len(selector.get_rules())
        selector.add_rule(
            TrafficRule(
                "r1", TrafficScope.LOCAL, MatchType.CIDR, "10.0.0.0/8", TrafficAction.INTERCEPT
            )
        )
        rules_copy = selector.get_rules()
        rules_copy.clear()
        assert len(selector.get_rules()) == initial_count + 1

    def test_clear_rules(self):
        selector = TrafficSelector()
        selector.add_rule(
            TrafficRule(
                "extra", TrafficScope.LOCAL, MatchType.CIDR, "10.0.0.0/8", TrafficAction.INTERCEPT
            )
        )
        count_after_add = len(selector.get_rules())
        selector.remove_rule("extra")
        assert len(selector.get_rules()) == count_after_add - 1

    class TestCreateRequestInfo:
        """create_request_info helper."""

        def test_auto_detect_local_private_ip(self):
            info = create_request_info(client_ip="192.168.1.10")
            assert info.is_local is True

        def test_auto_detect_local_loopback(self):
            info = create_request_info(client_ip="127.0.0.1")
            assert info.is_local is True

        def test_auto_detect_external_ip(self):
            info = create_request_info(client_ip="8.8.8.8")
            assert info.is_local is False

        def test_auto_detect_invalid_ip(self):
            info = create_request_info(client_ip="bad-ip")
            assert info.is_local is False

        def test_explicit_is_local_overrides(self):
            info = create_request_info(client_ip="8.8.8.8", is_local=True)
            assert info.is_local is True

        def test_all_fields(self):
            info = create_request_info(
                client_ip="10.0.0.5",
                hostname="device.local",
                vendor="shelly",
                device_id="shelly-001",
                url="http://device.local/rpc/Switch.Set",
                path="/rpc/Switch.Set",
            )
            assert info.client_ip == "10.0.0.5"
            assert info.hostname == "device.local"
            assert info.vendor == "shelly"
            assert info.device_id == "shelly-001"
            assert info.url == "http://device.local/rpc/Switch.Set"
            assert info.path == "/rpc/Switch.Set"
            assert info.is_local is True
