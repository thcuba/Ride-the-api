"""
On-the-Fly Modification Engine - Real-time interception and modification
of device requests and cloud responses based on configurable rules.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from core.config import get_config_manager


@dataclass
class ResponseRecord:
    """Record of an adapter response for modification processing."""
    source: str
    status_code: int
    headers: dict[str, str]
    body: dict[str, Any] | None
    latency_ms: float
    timestamp: datetime
    metadata: dict[str, Any]

logger = logging.getLogger(__name__)


class ModificationAction(str, Enum):
    """Types of modifications that can be applied."""
    MODIFY = "modify"        # Change field value
    BLOCK = "block"          # Block the request/response entirely
    INJECT = "inject"        # Add new field/header
    REPLACE = "replace"      # Replace entire body
    REDIRECT = "redirect"    # Redirect to different endpoint
    DELAY = "delay"          # Add artificial delay


class ModificationOperation(str, Enum):
    """Operations for field modification."""
    SET = "set"              # Set to specific value
    ADD = "add"              # Add numeric value
    MULTIPLY = "multiply"    # Multiply numeric value
    CLAMP = "clamp"          # Clamp to min/max range
    REPLACE = "replace"      # String replace
    REMOVE = "remove"        # Remove field


@dataclass
class ModificationRule:
    """A single modification rule."""
    name: str
    match_vendor: str | None = None
    match_device_type: str | None = None
    match_intent: str | None = None
    match_field_path: str | None = None  # JSONPath expression
    match_value: Any = None
    match_headers: dict[str, str] | None = None
    match_method: str | None = None
    match_path_pattern: str | None = None  # regex pattern

    action: ModificationAction = ModificationAction.MODIFY
    action_params: dict[str, Any] = field(default_factory=dict)

    priority: int = 10
    enabled: bool = True
    direction: str = "request"  # "request" | "response" | "both"

    # Compiled patterns
    _path_regex: re.Pattern | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.match_path_pattern:
            self._path_regex = re.compile(self.match_path_pattern)

    def matches(self, intercepted: InterceptedMessage, direction: str) -> bool:
        """Check if this rule matches the intercepted message."""
        if not self.enabled:
            return False

        # Check direction
        if self.direction != "both" and self.direction != direction:
            return False

        # Vendor match
        if self.match_vendor and intercepted.vendor != self.match_vendor:
            return False

        # Device type match
        if self.match_device_type and intercepted.device_type != self.match_device_type:
            return False

        # Intent match
        if self.match_intent and intercepted.intent != self.match_intent:
            return False

        # Method match
        if self.match_method and intercepted.method != self.match_method:
            return False

        # Path pattern match
        if self._path_regex and not self._path_regex.search(intercepted.path or ''):
            return False

        # Headers match
        if self.match_headers:
            for k, v in self.match_headers.items():
                if intercepted.headers.get(k.lower()) != v:
                    return False

        # Field path match
        if self.match_field_path:
            value = self._get_json_path(intercepted.body, self.match_field_path)
            if value is None:
                return False
            if self.match_value is not None and value != self.match_value:
                return False

        return True

    def _get_json_path(self, obj: Any, path: str) -> Any:
        """Simple JSONPath-like getter (supports $.field.subfield[0])."""
        if not obj:
            return None

        # Remove leading $.
        if path.startswith('$'):
            path = path[1:]
        if path.startswith('.'):
            path = path[1:]

        parts = path.split('.')
        current = obj

        for part in parts:
            if '[' in part and ']' in part:
                # Array access
                key = part[:part.index('[')]
                idx = int(part[part.index('[')+1:part.index(']')])
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    def _set_json_path(self, obj: Any, path: str, value: Any) -> bool:
        """Simple JSONPath-like setter."""
        if not obj:
            return False

        if path.startswith('$'):
            path = path[1:]
        if path.startswith('.'):
            path = path[1:]

        parts = path.split('.')
        current = obj

        for i, part in enumerate(parts[:-1]):
            if '[' in part and ']' in part:
                key = part[:part.index('[')]
                idx = int(part[part.index('[')+1:part.index(']')])
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return False
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return False
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False

        # Set final part
        last = parts[-1]
        if '[' in last and ']' in last:
            key = last[:last.index('[')]
            idx = int(last[last.index('[')+1:last.index(']')])
            if isinstance(current, dict) and key in current and isinstance(current[key], list):
                if 0 <= idx < len(current[key]):
                    current[key][idx] = value
                    return True
        elif isinstance(current, dict):
            current[last] = value
            return True

        return False

    def apply(self, intercepted: InterceptedMessage) -> InterceptedMessage:
        """Apply this modification rule to the intercepted message."""
        # Create a copy to avoid mutating original
        import copy
        modified = copy.deepcopy(intercepted)

        if self.action == ModificationAction.BLOCK:
            # Signal to block
            modified.blocked = True
            modified.block_reason = f"Rule: {self.name}"
            return modified

        if self.action == ModificationAction.MODIFY:
            if self.match_field_path:
                op = self.action_params.get('operation', 'set')

                if op == ModificationOperation.SET:
                    new_value = self.action_params.get('value')
                    self._set_json_path(modified.body, self.match_field_path, new_value)

                elif op == ModificationOperation.ADD:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, (int, float)):
                        amount = self.action_params.get('amount', 0)
                        self._set_json_path(modified.body, self.match_field_path, current + amount)

                elif op == ModificationOperation.MULTIPLY:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, (int, float)):
                        factor = self.action_params.get('factor', 1)
                        self._set_json_path(modified.body, self.match_field_path, current * factor)

                elif op == ModificationOperation.CLAMP:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, (int, float)):
                        min_val = self.action_params.get('min')
                        max_val = self.action_params.get('max')
                        if min_val is not None:
                            current = max(current, min_val)
                        if max_val is not None:
                            current = min(current, max_val)
                        self._set_json_path(modified.body, self.match_field_path, current)

                elif op == ModificationOperation.REPLACE:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, str):
                        old = self.action_params.get('old', '')
                        new = self.action_params.get('new', '')
                        self._set_json_path(modified.body, self.match_field_path, current.replace(old, new))

                elif op == ModificationOperation.REMOVE:
                    # Set to None to indicate removal
                    self._set_json_path(modified.body, self.match_field_path, None)

        elif self.action == ModificationAction.INJECT:
            field_path = self.action_params.get('field_path')
            value = self.action_params.get('value')
            if field_path and value is not None:
                self._set_json_path(modified.body, field_path, value)

        elif self.action == ModificationAction.REPLACE:
            new_body = self.action_params.get('body')
            if new_body is not None:
                modified.body = new_body

        elif self.action == ModificationAction.REDIRECT:
            new_path = self.action_params.get('path')
            new_host = self.action_params.get('host')
            if new_path:
                modified.path = new_path
            if new_host:
                modified.headers['host'] = new_host

        elif self.action == ModificationAction.DELAY:
            # Add delay metadata (handled by proxy)
            delay_ms = self.action_params.get('delay_ms', 0)
            modified.metadata['artificial_delay_ms'] = delay_ms

        # Track modification
        modified.modifications.append({
            'rule': self.name,
            'action': self.action.value,
            'timestamp': datetime.now(UTC).isoformat(),
        })

        return modified


@dataclass
class InterceptedMessage:
    """Represents an intercepted request or response."""
    direction: str  # "request" | "response"
    device_id: str
    vendor: str
    device_type: str
    intent: str
    method: str
    path: str
    headers: dict[str, str]
    body: dict | list | str | None
    query_params: dict[str, str] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    # Modification tracking
    blocked: bool = False
    block_reason: str | None = None
    modifications: list[dict] = field(default_factory=list)

    @classmethod
    def from_request(cls, intercepted: InterceptedRequest) -> InterceptedMessage:
        """Create from InterceptedRequest."""
        return cls(
            direction="request",
            device_id=intercepted.device_id,
            vendor=intercepted.vendor,
            device_type=intercepted.metadata.get('device_type', 'unknown'),
            intent=intercepted.parsed_intent.value if intercepted.parsed_intent else 'unknown',
            method=intercepted.method,
            path=intercepted.path,
            headers={k.lower(): v for k, v in intercepted.headers.items()},
            body=intercepted.body,
            query_params=intercepted.query_params,
            metadata=intercepted.metadata,
        )

    @classmethod
    def from_response(cls, response: ResponseRecord) -> InterceptedMessage:
        """Create from ResponseRecord."""
        return cls(
            direction="response",
            device_id=response.metadata.get('device_id', ''),
            vendor=response.metadata.get('vendor', ''),
            device_type=response.metadata.get('device_type', 'unknown'),
            intent=response.metadata.get('intent', 'unknown'),
            method=response.metadata.get('method', ''),
            path=response.metadata.get('path', ''),
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.body,
            metadata=response.metadata,
        )


class ModificationEngine:
    """
    Real-time modification engine for request/response transformation.
    Rules are evaluated in priority order (highest first).
    """

    def __init__(self, config_manager=None):
        self.config_manager = config_manager or get_config_manager()
        self._rules: list[ModificationRule] = []
        self._audit_log: list[dict] = []
        self._max_audit = 10000
        self._load_rules()

        # Register config change callback
        self.config_manager.add_change_callback(self._on_config_change)

    def _load_rules(self):
        """Load modification rules from configuration."""
        config = self.config_manager.config
        mod_config = getattr(config, 'modification', None)

        if not mod_config or not getattr(mod_config, 'enabled', True):
            self._rules = []
            logger.info("Modification engine disabled")
            return

        rules_config = getattr(mod_config, 'rules', [])
        self._rules = []

        for rule_data in rules_config:
            try:
                rule = ModificationRule(
                    name=rule_data.get('name', 'unnamed'),
                    match_vendor=rule_data.get('match_vendor'),
                    match_device_type=rule_data.get('match_device_type'),
                    match_intent=rule_data.get('match_intent'),
                    match_field_path=rule_data.get('match_field_path'),
                    match_value=rule_data.get('match_value'),
                    match_headers=rule_data.get('match_headers'),
                    match_method=rule_data.get('match_method'),
                    match_path_pattern=rule_data.get('match_path_pattern'),
                    action=ModificationAction(rule_data.get('action', 'modify')),
                    action_params=rule_data.get('action_params', {}),
                    priority=rule_data.get('priority', 10),
                    enabled=rule_data.get('enabled', True),
                    direction=rule_data.get('direction', 'request'),
                )
                self._rules.append(rule)
            except Exception as e:
                logger.error(f"Failed to load modification rule {rule_data.get('name')}: {e}")

        # Sort by priority (highest first)
        self._rules.sort(key=lambda r: -r.priority)

        logger.info(f"Loaded {len(self._rules)} modification rules")

    def _on_config_change(self, new_config):
        """Reload rules on config change."""
        logger.info("Modification config changed, reloading rules")
        self._load_rules()

    def add_rule(self, rule: ModificationRule):
        """Add a new rule dynamically."""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: -r.priority)

    def remove_rule(self, name: str) -> bool:
        """Remove a rule by name."""
        for i, rule in enumerate(self._rules):
            if rule.name == name:
                self._rules.pop(i)
                return True
        return False

    def get_rules(self) -> list[ModificationRule]:
        """Get all current rules."""
        return self._rules.copy()

    def process_request(self, intercepted: InterceptedRequest) -> tuple[InterceptedRequest, bool]:
        """
        Process an intercepted request through modification rules.
        Returns (modified_request, was_modified).
        """
        msg = InterceptedMessage.from_request(intercepted)

        for rule in self._rules:
            if rule.matches(msg, "request"):
                original_body = json.dumps(msg.body) if msg.body else None
                msg = rule.apply(msg)

                # Log modification
                self._log_modification(rule, msg, original_body, None)

                if msg.blocked:
                    logger.warning(f"Request blocked by rule {rule.name}: {msg.block_reason}")
                    break

        # Apply modifications back to intercepted request
        modified_intercepted = self._apply_to_request(intercepted, msg)
        was_modified = len(msg.modifications) > 0

        return modified_intercepted, was_modified

    def process_response(self, intercepted: InterceptedRequest, response: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """
        Process an adapter response through modification rules.
        Returns (modified_response, was_modified).
        """
        # Create message from response
        response_record = ResponseRecord(
            source="edge",
            status_code=200,
            headers={},
            body=response,
            latency_ms=0,
            timestamp=datetime.now(UTC),
            metadata={
                'device_id': intercepted.device_id,
                'vendor': intercepted.vendor,
                'device_type': intercepted.metadata.get('device_type', 'unknown'),
                'intent': intercepted.parsed_intent.value if intercepted.parsed_intent else 'unknown',
                'method': intercepted.method,
                'path': intercepted.path,
            }
        )

        msg = InterceptedMessage.from_response(response_record)

        for rule in self._rules:
            if rule.matches(msg, "response"):
                original_body = json.dumps(msg.body) if msg.body else None
                msg = rule.apply(msg)

                # Log modification
                self._log_modification(rule, msg, original_body, None)

                if msg.blocked:
                    logger.warning(f"Response blocked by rule {rule.name}: {msg.block_reason}")
                    break

        # Apply modifications back to response
        modified_response = self._apply_to_response(response, msg)
        was_modified = len(msg.modifications) > 0

        return modified_response, was_modified

    def _apply_to_request(self, original: InterceptedRequest, msg: InterceptedMessage) -> InterceptedRequest:
        """Apply message modifications back to InterceptedRequest."""
        if msg.body is not original.body:
            original.body = msg.body
        if msg.path != original.path:
            original.path = msg.path
        if msg.headers:
            original.headers = {k.title(): v for k, v in msg.headers.items()}
        original.metadata = msg.metadata
        original.modifications = msg.modifications
        if msg.blocked:
            original.blocked = True
            original.block_reason = msg.block_reason
        return original

    def _apply_to_response(self, original: dict[str, Any], msg: InterceptedMessage) -> dict[str, Any]:
        """Apply message modifications back to response dict."""
        if msg.body is not None:
            original = msg.body
        if msg.modifications:
            original = dict(original)
            original['modifications'] = msg.modifications
        if msg.blocked:
            original = {"success": False, "error": msg.block_reason}
        return original

    def _log_modification(self, rule: ModificationRule, msg: InterceptedMessage,
                         original_body: str | None, original_headers: dict | None):
        """Log modification to audit trail."""
        entry = {
            'timestamp': datetime.now(UTC).isoformat(),
            'rule': rule.name,
            'action': rule.action.value,
            'device_id': msg.device_id,
            'vendor': msg.vendor,
            'direction': msg.direction,
            'original_body': original_body,
            'modified_body': json.dumps(msg.body) if msg.body else None,
            'modifications': msg.modifications[-1] if msg.modifications else None,
        }

        self._audit_log.append(entry)
        if len(self._audit_log) > self._max_audit:
            self._audit_log = self._audit_log[-self._max_audit:]

        # Also write to file if configured
        config = self.config_manager.config
        mod_config = getattr(config, 'modification', None)
        if mod_config and getattr(mod_config, 'audit_log_enabled', True):
            audit_path = getattr(mod_config, 'audit_log_path', './logs/modifications.jsonl')
            try:
                import os
                os.makedirs(os.path.dirname(audit_path), exist_ok=True)
                with open(audit_path, 'a') as f:
                    f.write(json.dumps(entry) + '\n')
            except Exception as e:
                logger.error(f"Failed to write audit log: {e}")

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        """Get recent modification audit log."""
        return self._audit_log[-limit:]


# Global instance
_modification_engine: ModificationEngine | None = None


def get_modification_engine() -> ModificationEngine:
    """Get or create global modification engine instance."""
    global _modification_engine
    if _modification_engine is None:
        _modification_engine = ModificationEngine()
    return _modification_engine
