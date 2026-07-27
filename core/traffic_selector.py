"""
Traffic Selection Engine - Determines intercept vs passthrough for incoming requests.
Rules can be managed via UI/API and are evaluated in priority order.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from core.config import get_config_manager

logger = logging.getLogger(__name__)


class TrafficScope(str, Enum):
    """Scope of the traffic rule."""
    LOCAL = "local"           # Device on local network
    EXTERNAL = "external"     # Cloud/Internet traffic


class TrafficAction(str, Enum):
    """Action to take on matching traffic."""
    INTERCEPT = "intercept"    # Process through edge AI
    PASSTHROUGH = "passthrough"  # Forward directly to destination


class MatchType(str, Enum):
    """Type of matching for traffic rules."""
    CIDR = "cidr"              # IP CIDR range (local only)
    HOSTNAME = "hostname"      # Hostname pattern with wildcards (external only)
    VENDOR = "vendor"          # Vendor code (ty, tl, zh, hr)
    DEVICE_ID = "device_id"    # Specific device ID


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
    _compiled_pattern: Optional[re.Pattern] = field(default=None, init=False, repr=False)
    _cidr_network: Optional[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default=None, init=False, repr=False)
    
    def __post_init__(self):
        """Compile patterns after initialization."""
        if self.match_type == MatchType.HOSTNAME:
            # Convert wildcard pattern to regex
            pattern = self.match_value.replace(".", r"\.").replace("*", ".*")
            self._compiled_pattern = re.compile(f"^{pattern}$", re.IGNORECASE)
        elif self.match_type == MatchType.CIDR:
            self._cidr_network = ipaddress.ip_network(self.match_value, strict=False)
    
    def matches(self, request_info: TrafficRequestInfo) -> bool:
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
                try:
                    return ipaddress.ip_address(request_info.client_ip) in self._cidr_network
                except ValueError:
                    return False
            return False
        
        elif self.match_type == MatchType.HOSTNAME:
            if request_info.hostname and self._compiled_pattern:
                return bool(self._compiled_pattern.match(request_info.hostname))
            return False
        
        elif self.match_type == MatchType.VENDOR:
            return request_info.vendor and request_info.vendor.lower() == self.match_value.lower()
        
        elif self.match_type == MatchType.DEVICE_ID:
            return request_info.device_id and request_info.device_id == self.match_value
        
        return False


@dataclass
class TrafficRequestInfo:
    """Information extracted from request for rule matching."""
    client_ip: str
    hostname: Optional[str] = None
    vendor: Optional[str] = None
    device_id: Optional[str] = None
    is_local: bool = False
    url: Optional[str] = None
    path: Optional[str] = None


class TrafficSelector:
    """
    Evaluates traffic selection rules to determine intercept vs passthrough.
    Rules are evaluated in priority order (highest first).
    """
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or get_config_manager()
        self.rules: list[TrafficRule] = []
        self._default_action = TrafficAction.INTERCEPT
        self._load_rules()
        
        # Watch for config changes
        self.config_manager.add_change_callback(self._on_config_change)
    
    def _load_rules(self):
        """Load rules from configuration."""
        config = self.config_manager.config
        ts_config = getattr(config, 'traffic_selection', None)
        
        if not ts_config:
            logger.warning("No traffic_selection config found, using defaults")
            self._default_action = TrafficAction.INTERCEPT
            self.rules = []
            return
        
        # Default action
        default = getattr(ts_config, 'default_action', 'intercept')
        self._default_action = TrafficAction(default)
        
        # Parse rules
        rules = getattr(ts_config, 'rules', [])
        self.rules = []
        
        for rule_data in rules:
            try:
                rule = TrafficRule(
                    name=rule_data.get('name', 'unnamed'),
                    scope=TrafficScope(rule_data.get('scope', 'local')),
                    match_type=MatchType(rule_data.get('match_type', 'cidr')),
                    match_value=rule_data.get('match_value', ''),
                    action=TrafficAction(rule_data.get('action', 'intercept')),
                    priority=rule_data.get('priority', 10),
                    enabled=rule_data.get('enabled', True),
                )
                self.rules.append(rule)
            except Exception as e:
                logger.error(f"Failed to parse traffic rule {rule_data}: {e}")
        
        # Sort by priority (highest first)
        self.rules.sort(key=lambda r: r.priority, reverse=True)
        
        logger.info(f"Loaded {len(self.rules)} traffic selection rules, default: {self._default_action.value}")
    
    def _on_config_change(self, new_config):
        """Reload rules when config changes."""
        logger.info("Traffic selection config changed, reloading rules")
        self._load_rules()
    
    def evaluate(self, request_info: TrafficRequestInfo) -> TrafficAction:
        """
        Evaluate request against rules and return action.
        First matching rule wins.
        """
        for rule in self.rules:
            if rule.matches(request_info):
                logger.debug(f"Traffic rule '{rule.name}' matched: {rule.action.value}")
                return rule.action
        
        logger.debug(f"No rule matched, using default: {self._default_action.value}")
        return self._default_action
    
    def get_rules(self) -> list[TrafficRule]:
        """Get all current rules."""
        return self.rules.copy()
    
    def add_rule(self, rule: TrafficRule) -> bool:
        """Add a new rule (will be re-sorted by priority)."""
        self.rules.append(rule)
        self.rules.sort(key=lambda r: r.priority, reverse=True)
        return True
    
    def remove_rule(self, name: str) -> bool:
        """Remove rule by name."""
        for i, rule in enumerate(self.rules):
            if rule.name == name:
                self.rules.pop(i)
                return True
        return False
    
    def update_rule(self, name: str, **kwargs) -> bool:
        """Update rule properties."""
        for rule in self.rules:
            if rule.name == name:
                for key, value in kwargs.items():
                    if hasattr(rule, key):
                        setattr(rule, key, value)
                # Re-sort if priority changed
                if 'priority' in kwargs:
                    self.rules.sort(key=lambda r: r.priority, reverse=True)
                return True
        return False


# Global instance
_traffic_selector: TrafficSelector | None = None


def get_traffic_selector() -> TrafficSelector:
    """Get or create global traffic selector instance."""
    global _traffic_selector
    if _traffic_selector is None:
        _traffic_selector = TrafficSelector()
    return _traffic_selector


def create_request_info(
    client_ip: str,
    hostname: str | None = None,
    vendor: str | None = None,
    device_id: str | None = None,
    url: str | None = None,
    path: str | None = None,
    is_local: bool | None = None,
) -> TrafficRequestInfo:
    """Create TrafficRequestInfo with automatic local detection."""
    if is_local is None:
        # Auto-detect local IP
        try:
            ip = ipaddress.ip_address(client_ip)
            is_local = ip.is_private or ip.is_loopback
        except ValueError:
            is_local = False
    
    return TrafficRequestInfo(
        client_ip=client_ip,
        hostname=hostname,
        vendor=vendor,
        device_id=device_id,
        is_local=is_local,
        url=url,
        path=path,
    )