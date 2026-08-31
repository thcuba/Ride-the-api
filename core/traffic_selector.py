"""
Traffic Selection Engine - Determines intercept vs passthrough for incoming requests.
Rules can be managed via UI/API and are evaluated in priority order.
"""

from __future__ import annotations

import contextlib
import fnmatch
import ipaddress
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum

from core.config import get_config_manager

logger = logging.getLogger(__name__)


class TrafficScope(str, Enum):  # noqa: UP042
    """Scope of the traffic rule."""

    LOCAL = "local"  # Device on local network
    EXTERNAL = "external"  # Cloud/Internet traffic


class TrafficAction(str, Enum):  # noqa: UP042
    """Action to take on matching traffic."""

    INTERCEPT = "intercept"  # Process through edge AI
    PASSTHROUGH = "passthrough"  # Forward directly to destination


class MatchType(str, Enum):  # noqa: UP042
    """Type of matching for traffic rules."""

    CIDR = "cidr"  # IP CIDR range (local only)
    HOSTNAME = "hostname"  # Hostname pattern with wildcards (external only)
    VENDOR = "vendor"  # Vendor code (ty, tl, zh, hr)
    DEVICE_ID = "device_id"  # Specific device ID


@dataclass
class TrafficRule:
    """A single traffic selection rule."""

    name: str
    scope: TrafficScope
    match_type: MatchType
    match_value: str
    action: TrafficAction
    priority: int = 10
    enabled: bool = True

    # Compiled patterns for performance
    _compiled_pattern: re.Pattern | None = field(default=None, init=False, repr=False)
    _cidr_network: ipaddress.IPv4Network | ipaddress.IPv6Network | None = field(
        default=None, init=False, repr=False
    )
    _match_value_lower: str = field(default="", init=False, repr=False)

    def _recompile(self) -> None:
        """(Re)compile the cached regex/CIDR/match_value from current match_value.

        Called at init and again by ``update_rule`` whenever ``match_value``
        or ``match_type`` changes, so an edited rule does not keep matching
        against its stale compiled pattern.
        """
        self._compiled_pattern = None
        self._cidr_network = None
        self._match_value_lower = self.match_value.lower() if self.match_value else ""
        if self.match_type == MatchType.CIDR:
            self._cidr_network = ipaddress.ip_network(self.match_value, strict=False)
        elif self.match_type == MatchType.HOSTNAME:
            # Convert wildcard pattern to regex via fnmatch (stdlib)
            pattern = fnmatch.translate(self.match_value)
            self._compiled_pattern = re.compile(pattern, re.IGNORECASE)

    def __post_init__(self) -> None:
        """Compile patterns after initialization."""
        self._recompile()

    def matches(self, request_info: TrafficRequestInfo) -> bool:  # noqa: C901, PLR0911
        """Check if this rule matches the given request."""
        if not self.enabled:
            return False

        # Scope must match
        if self.scope == TrafficScope.LOCAL and not request_info.is_local:
            return False
        if self.scope == TrafficScope.EXTERNAL and request_info.is_local:
            return False

        # Match based on type
        if self.match_type == MatchType.CIDR:
            if request_info.client_ip and self._cidr_network:
                # Use cached ip_obj to avoid repeated string parsing per rule match
                ip_obj = request_info.ip_obj
                if ip_obj is not None:
                    return ip_obj in self._cidr_network
            return False

        if self.match_type == MatchType.HOSTNAME:
            if request_info.hostname and self._compiled_pattern:
                return bool(self._compiled_pattern.match(request_info.hostname))
            return False

        if self.match_type == MatchType.VENDOR:
            return bool(
                request_info.vendor and request_info.vendor.lower() == self._match_value_lower
            )

        if self.match_type == MatchType.DEVICE_ID:
            return request_info.device_id and request_info.device_id == self.match_value

        return False


@dataclass
class TrafficRequestInfo:
    """Information extracted from request for rule matching."""

    client_ip: str
    hostname: str | None = None
    vendor: str | None = None
    device_id: str | None = None
    is_local: bool = False
    url: str | None = None
    path: str | None = None
    _ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None = field(
        default=None, init=False, repr=False
    )

    @property
    def ip_obj(self) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        """Cached ipaddress object for fast CIDR matching (eliminates repeated string parsing)."""
        if self._ip_obj is None and self.client_ip:
            with contextlib.suppress(ValueError):
                self._ip_obj = ipaddress.ip_address(self.client_ip)
        return self._ip_obj


class TrafficSelector:
    """
    Evaluates traffic selection rules to determine intercept vs passthrough.
    Rules are evaluated in priority order (highest first).
    """

    def __init__(self, config_manager=None) -> None:
        self.config_manager = config_manager or get_config_manager()
        self._lock = threading.RLock()
        self.rules: list[TrafficRule] = []
        self._default_action = TrafficAction.INTERCEPT
        self._load_rules()

        # Watch for config changes
        self.config_manager.register_callback(self._on_config_change)

    def _load_rules(self):
        """Load rules from configuration."""
        config = self.config_manager.config
        ts_config = getattr(config, "traffic_selection", None)

        with self._lock:
            if not ts_config:
                logger.warning("No traffic_selection config found, using defaults")
                self._default_action = TrafficAction.INTERCEPT
                self.rules = []
                return

            # Default action
            default = getattr(ts_config, "default_action", "intercept")
            self._default_action = TrafficAction(default)

            # Parse rules
            rules = getattr(ts_config, "rules", [])
            new_rules: list[TrafficRule] = []

            for rule_data in rules:
                try:
                    # Handle both Pydantic models and dicts
                    if isinstance(rule_data, dict):
                        rule = TrafficRule(
                            name=rule_data.get("name", "unnamed"),
                            scope=TrafficScope(rule_data.get("scope", "local")),
                            match_type=MatchType(rule_data.get("match_type", "cidr")),
                            match_value=rule_data.get("match_value", ""),
                            action=TrafficAction(rule_data.get("action", "intercept")),
                            priority=rule_data.get("priority", 10),
                            enabled=rule_data.get("enabled", True),
                        )
                    else:
                        rule = TrafficRule(
                            name=getattr(rule_data, "name", "unnamed"),
                            scope=TrafficScope(getattr(rule_data, "scope", "local")),
                            match_type=MatchType(getattr(rule_data, "match_type", "cidr")),
                            match_value=getattr(rule_data, "match_value", ""),
                            action=TrafficAction(getattr(rule_data, "action", "intercept")),
                            priority=getattr(rule_data, "priority", 10),
                            enabled=getattr(rule_data, "enabled", True),
                        )
                    new_rules.append(rule)
                except Exception as e:
                    logger.error(f"Failed to parse traffic rule {rule_data}: {e}")  # noqa: TRY400

            # Sort by priority (highest first), then atomically publish the new list.
            new_rules.sort(key=lambda r: r.priority, reverse=True)
            self.rules = new_rules

            logger.info(
                f"Loaded {len(self.rules)} traffic selection rules, "
                f"default: {self._default_action.value}"
            )

    def _on_config_change(self, _new_config):
        """Reload rules when config changes."""
        logger.info("Traffic selection config changed, reloading rules")
        self._load_rules()

    def evaluate(self, request_info: TrafficRequestInfo) -> TrafficAction:
        """
        Evaluate request against rules and return action.
        First matching rule wins.
        """
        with self._lock:
            for rule in self.rules:
                if rule.matches(request_info):
                    logger.debug(f"Traffic rule '{rule.name}' matched: {rule.action.value}")
                    return rule.action

        logger.debug(f"No rule matched, using default: {self._default_action.value}")
        return self._default_action

    def get_rules(self) -> list[TrafficRule]:
        """Get all current rules."""
        with self._lock:
            return self.rules.copy()

    @property
    def default_action(self) -> TrafficAction:
        """Get the default traffic action."""
        return self._default_action

    def add_rule(self, rule: TrafficRule) -> bool:
        """Add a new rule (will be re-sorted by priority)."""
        try:
            rule._recompile()  # validate the match definition compiles
        except ValueError as exc:
            logger.error(f"Rejected rule {rule.name}: invalid match definition: {exc}")  # noqa: TRY400
            return False
        with self._lock:
            self.rules.append(rule)
            self.rules.sort(key=lambda r: r.priority, reverse=True)
        return True

    def remove_rule(self, name: str) -> bool:
        """Remove rule by name."""
        with self._lock:
            for i, rule in enumerate(self.rules):
                if rule.name == name:
                    self.rules.pop(i)
                    return True
        return False

    def update_rule(self, name: str, **kwargs) -> bool:
        """Update rule properties."""
        with self._lock:
            for rule in self.rules:
                if rule.name == name:
                    # Snapshot match fields so we can roll back when a new
                    # match definition does not compile (bad regex/CIDR).
                    old_match = (rule.match_type, rule.match_value)
                    for key, value in kwargs.items():
                        if hasattr(rule, key):
                            setattr(rule, key, value)
                    # Recompile cached regex/CIDR if the match definition changed.
                    if any(k in kwargs for k in ("match_value", "match_type")):
                        try:
                            rule._recompile()
                        except ValueError as exc:
                            rule.match_type, rule.match_value = old_match
                            rule._recompile()  # restore the old compiled state
                            logger.error(  # noqa: TRY400
                                f"Rejected update of rule {name}: invalid match definition: {exc}"
                            )
                            return False
                    # Re-sort if priority changed
                    if "priority" in kwargs:
                        self.rules.sort(key=lambda r: r.priority, reverse=True)
                    return True
        return False


# Global instance
_traffic_selector: TrafficSelector | None = None


def get_traffic_selector() -> TrafficSelector:
    """Get or create global traffic selector instance."""
    global _traffic_selector  # noqa: PLW0603
    if _traffic_selector is None:
        _traffic_selector = TrafficSelector()
    return _traffic_selector


def create_request_info(  # noqa: PLR0913
    client_ip: str,
    hostname: str | None = None,
    vendor: str | None = None,
    device_id: str | None = None,
    url: str | None = None,
    path: str | None = None,
    is_local: bool | None = None,
) -> TrafficRequestInfo:
    """Create TrafficRequestInfo with automatic local detection."""
    req_info = TrafficRequestInfo(
        client_ip=client_ip,
        hostname=hostname,
        vendor=vendor,
        device_id=device_id,
        is_local=False,
        url=url,
        path=path,
    )
    if is_local is None:
        # Auto-detect local IP using cached ip_obj
        ip = req_info.ip_obj
        if ip is not None:
            req_info.is_local = ip.is_private or ip.is_loopback
        else:
            req_info.is_local = False
    else:
        req_info.is_local = is_local

    return req_info
