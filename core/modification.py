"""
On-the-Fly Modification Engine - Real-time interception and modification
of device requests and cloud responses based on configurable rules.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

import dpath

if TYPE_CHECKING:
    from adapters.base import InterceptedRequest

from core.atomic_io import append_jsonl


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


@functools.lru_cache(maxsize=2048)
def _dot_to_dpath(path: str) -> str:
    """Convert dot/bracket path notation ('a.b[0].c') to dpath slash notation.

    dpath uses '/' as its default separator; map '.' and '[0]' → '/', and
    drop the closing ']'. Array indexes become segments (e.g. ``items/0/name``).

    Memoized with lru_cache for fast O(1) path conversion during rule evaluation.
    """
    p = path.lstrip("$")
    p = p.replace("[", "/").replace("]", "")
    return p.replace(".", "/")


def _rule_field(data: Any, key: str, default: Any = None) -> Any:  # noqa: ANN401
    """Read a rule field from either a dict or a Pydantic/object instance."""
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


class ModificationAction(StrEnum):
    """Types of modifications that can be applied."""

    MODIFY = "modify"  # Change field value
    BLOCK = "block"  # Block the request/response entirely
    INJECT = "inject"  # Add new field/header
    REPLACE = "replace"  # Replace entire body
    REDIRECT = "redirect"  # Redirect to different endpoint
    DELAY = "delay"  # Add artificial delay


class ModificationOperation(str, Enum):  # noqa: UP042
    """Operations for field modification."""

    SET = "set"  # Set to specific value
    ADD = "add"  # Add numeric value
    MULTIPLY = "multiply"  # Multiply numeric value
    CLAMP = "clamp"  # Clamp to min/max range
    REPLACE = "replace"  # String replace
    REMOVE = "remove"  # Remove field


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

    # Compiled patterns and precomputed lookups
    _path_regex: re.Pattern | None = field(default=None, init=False, repr=False)
    _match_headers_lower: dict[str, str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.match_path_pattern:
            self._path_regex = re.compile(self.match_path_pattern)
        if self.match_headers:
            self._match_headers_lower = {k.lower(): v for k, v in self.match_headers.items()}

    def matches(self, intercepted: InterceptedMessage, direction: str) -> bool:  # noqa: C901, PLR0911, PLR0912
        """Check if this rule matches the intercepted message."""
        if not self.enabled:
            return False

        # Check direction
        if self.direction not in ("both", direction):
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
        if self._path_regex and not self._path_regex.search(intercepted.path or ""):
            return False

        # Headers match (uses pre-computed lowercased keys to avoid allocations in hot path)
        if self._match_headers_lower:
            for k, v in self._match_headers_lower.items():
                if intercepted.headers.get(k) != v:
                    return False

        # Field path match
        if self.match_field_path:
            value = self._get_json_path(intercepted.body, self.match_field_path)
            if value is None:
                return False
            if self.match_value is not None and value != self.match_value:
                return False

        return True

    def _get_json_path(self, obj: Any, path: str) -> Any:  # noqa: ANN401
        """Simple JSONPath-like getter via dpath (supports $.field.subfield[0])."""
        if not obj:
            return None
        try:
            return dpath.get(obj, _dot_to_dpath(path), separator="/")
        except (KeyError, TypeError, IndexError):
            return None

    def _set_json_path(self, obj: Any, path: str, value: Any) -> bool:  # noqa: ANN401
        """Simple JSONPath-like setter via dpath."""
        if not obj:
            return False
        try:
            dpath.new(obj, _dot_to_dpath(path), value, separator="/")
        except (KeyError, TypeError, IndexError):
            return False
        return True

    def apply(self, intercepted: InterceptedMessage) -> InterceptedMessage:  # noqa: C901, PLR0912, PLR0915
        """Apply this modification rule to the intercepted message."""
        # Create a copy to avoid mutating original (uses fast custom copy (~2.9x faster))
        modified = intercepted.copy()

        if self.action == ModificationAction.BLOCK:
            # Signal to block
            modified.blocked = True
            modified.block_reason = f"Rule: {self.name}"
            return modified

        if self.action == ModificationAction.MODIFY:
            if self.match_field_path:
                op = self.action_params.get("operation", "set")

                if op == ModificationOperation.SET:
                    new_value = self.action_params.get("value")
                    self._set_json_path(modified.body, self.match_field_path, new_value)

                elif op == ModificationOperation.ADD:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, (int, float)):
                        amount = self.action_params.get("amount", 0)
                        self._set_json_path(modified.body, self.match_field_path, current + amount)

                elif op == ModificationOperation.MULTIPLY:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, (int, float)):
                        factor = self.action_params.get("factor", 1)
                        self._set_json_path(modified.body, self.match_field_path, current * factor)

                elif op == ModificationOperation.CLAMP:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, (int, float)):
                        min_val = self.action_params.get("min")
                        max_val = self.action_params.get("max")
                        if min_val is not None:
                            current = max(current, min_val)
                        if max_val is not None:
                            current = min(current, max_val)
                        self._set_json_path(modified.body, self.match_field_path, current)

                elif op == ModificationOperation.REPLACE:
                    current = self._get_json_path(modified.body, self.match_field_path)
                    if isinstance(current, str):
                        old = self.action_params.get("old", "")
                        new = self.action_params.get("new", "")
                        self._set_json_path(
                            modified.body, self.match_field_path, current.replace(old, new)
                        )

                elif op == ModificationOperation.REMOVE:
                    # Set to None to indicate removal
                    self._set_json_path(modified.body, self.match_field_path, None)

        elif self.action == ModificationAction.INJECT:
            field_path = self.action_params.get("field_path")
            value = self.action_params.get("value")
            if field_path and value is not None:
                self._set_json_path(modified.body, field_path, value)

        elif self.action == ModificationAction.REPLACE:
            new_body = self.action_params.get("body")
            if new_body is not None:
                modified.body = new_body

        elif self.action == ModificationAction.REDIRECT:
            new_path = self.action_params.get("path")
            new_host = self.action_params.get("host")
            if new_path:
                modified.path = new_path
            if new_host:
                modified.headers["host"] = new_host

        elif self.action == ModificationAction.DELAY:
            # Add delay metadata (handled by proxy)
            delay_ms = self.action_params.get("delay_ms", 0)
            modified.metadata["artificial_delay_ms"] = delay_ms

        # Track modification
        modified.modifications.append(
            {
                "rule": self.name,
                "action": self.action.value,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

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

    def copy(self) -> InterceptedMessage:
        """Efficiently copy InterceptedMessage (~2.9x faster than generic copy.deepcopy)."""
        body_copy = copy.deepcopy(self.body) if self.body is not None else None
        return InterceptedMessage(
            direction=self.direction,
            device_id=self.device_id,
            vendor=self.vendor,
            device_type=self.device_type,
            intent=self.intent,
            method=self.method,
            path=self.path,
            headers=self.headers.copy(),
            body=body_copy,
            query_params=self.query_params.copy(),
            metadata=self.metadata.copy(),
            blocked=self.blocked,
            block_reason=self.block_reason,
            modifications=copy.deepcopy(self.modifications),
        )

    @classmethod
    def from_request(cls, intercepted: InterceptedRequest) -> InterceptedMessage:
        """Create from InterceptedRequest."""
        _metadata = getattr(intercepted, "metadata", None) or {}
        _parsed = getattr(intercepted, "parsed_intent", None)
        return cls(
            direction="request",
            device_id=intercepted.device_id,
            vendor=getattr(intercepted, "vendor", ""),
            device_type=_metadata.get("device_type", "unknown"),
            intent=_parsed.value if _parsed else "unknown",
            method=intercepted.method,
            path=intercepted.path,
            headers={k.lower(): v for k, v in intercepted.headers.items()},
            body=intercepted.body,
            query_params=getattr(intercepted, "query_params", None) or {},
            metadata=_metadata,
        )

    @classmethod
    def from_response(cls, response: ResponseRecord) -> InterceptedMessage:
        """Create from ResponseRecord."""
        return cls(
            direction="response",
            device_id=response.metadata.get("device_id", ""),
            vendor=response.metadata.get("vendor", ""),
            device_type=response.metadata.get("device_type", "unknown"),
            intent=response.metadata.get("intent", "unknown"),
            method=response.metadata.get("method", ""),
            path=response.metadata.get("path", ""),
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.body,
            metadata=response.metadata,
        )


class ModificationEngine:
    """
    Real-time modification engine for request/response transformation.
    Rules are evaluated in priority order (highest first).
    """

    def __init__(self, config_manager=None) -> None:
        from core.config import get_config_manager  # lazy import to avoid circular dependency

        self.config_manager = config_manager or get_config_manager()
        self._rules: list[ModificationRule] = []
        self._audit_log: list[dict] = []
        self._max_audit = 10000
        self._load_rules()

        # Register config change callback
        self.config_manager.register_callback(self._on_config_change)

    def _load_rules(self):
        """Load modification rules from configuration."""
        config = self.config_manager.config
        mod_config = getattr(config, "modification", None)

        if not mod_config or not getattr(mod_config, "enabled", True):
            self._rules = []
            logger.info("Modification engine disabled")
            return

        rules_config = getattr(mod_config, "rules", [])
        self._rules = []

        for rule_data in rules_config:
            try:
                action_name = _rule_field(rule_data, "action", "modify")
                action = (
                    ModificationAction(action_name)
                    if isinstance(action_name, str)
                    else action_name
                )
                action_params = _rule_field(rule_data, "action_params", {})
                if not action_params:
                    action_params = self._translate_legacy_action_params(
                        action, rule_data
                    )
                rule = ModificationRule(
                    name=_rule_field(rule_data, "name", "unnamed"),
                    match_vendor=_rule_field(rule_data, "match_vendor"),
                    match_device_type=_rule_field(rule_data, "match_device_type"),
                    match_intent=_rule_field(rule_data, "match_intent"),
                    match_field_path=_rule_field(rule_data, "match_field_path"),
                    match_value=_rule_field(rule_data, "match_value"),
                    match_headers=_rule_field(rule_data, "match_headers")
                    or self._translate_legacy_match_headers(rule_data),
                    match_method=_rule_field(rule_data, "match_method"),
                    match_path_pattern=_rule_field(rule_data, "match_path_pattern"),
                    action=action,
                    action_params=action_params,
                    priority=_rule_field(rule_data, "priority", 10),
                    enabled=_rule_field(rule_data, "enabled", True),
                    direction=_rule_field(rule_data, "direction", "request"),
                )
                self._rules.append(rule)
            except Exception as e:
                logger.error(  # noqa: TRY400
                    "Failed to load modification rule "
                    f"{_rule_field(rule_data, 'name', 'unnamed')}: {e}"
                )
        # Sort by priority (highest first)
        self._rules.sort(key=lambda r: -r.priority)

        logger.info(f"Loaded {len(self._rules)} modification rules")

    def _translate_legacy_match_headers(self, rule_data) -> dict[str, str] | None:
        """Translate legacy ``match_type``/``match_value`` to a header match.

        Legacy rules matched on ``match_type`` (hostname/path/header/field)
        plus a ``match_value``. Only header-flavoured legacy matches map
        cleanly onto the engine's native ``match_headers`` — everything else
        is left to be handled by its native field or remains unmatched rather
        than silently matching everything.
        """
        match_type = _rule_field(rule_data, "match_type", "hostname")
        match_value = _rule_field(rule_data, "match_value")
        if match_type in ("hostname", "header") and match_value:
            return {"host": str(match_value)}
        return None

    def _translate_legacy_action_params(self, action: ModificationAction, rule_data) -> dict:
        """Translate legacy ``target_field``/``target_value`` into action_params."""
        target_field = _rule_field(rule_data, "target_field")
        target_value = _rule_field(rule_data, "target_value")
        params: dict = {}
        if action == ModificationAction.MODIFY and target_field:
            params = {"operation": "set", "value": target_value}
        elif action == ModificationAction.INJECT and target_field:
            params = {"field_path": target_field, "value": target_value}
        elif action == ModificationAction.REPLACE and target_value is not None:
            params = {"body": target_value}
        elif action == ModificationAction.REDIRECT and target_value is not None:
            params = {"path": str(target_value)}
        elif action == ModificationAction.DELAY:
            try:
                params = {"delay_ms": int(target_value or 0)}
            except (TypeError, ValueError):
                params = {}
        return params

    def _on_config_change(self, new_config):  # noqa: ARG002
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
                original_body = msg.body
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

    def process_response(
        self, intercepted: InterceptedRequest, response: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """
        Process an adapter response through modification rules.
        Returns (modified_response, was_modified).
        """
        _metadata = getattr(intercepted, "metadata", None) or {}
        _parsed = getattr(intercepted, "parsed_intent", None)
        # Create message from response
        response_record = ResponseRecord(
            source="edge",
            status_code=200,
            headers={},
            body=response,
            latency_ms=0,
            timestamp=datetime.now(UTC),
            metadata={
                "device_id": intercepted.device_id,
                "vendor": getattr(intercepted, "vendor", ""),
                "device_type": _metadata.get("device_type", "unknown"),
                "intent": _parsed.value if _parsed else "unknown",
                "method": intercepted.method,
                "path": intercepted.path,
            },
        )

        msg = InterceptedMessage.from_response(response_record)

        for rule in self._rules:
            if rule.matches(msg, "response"):
                original_body = msg.body
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

    def _apply_to_request(
        self, original: InterceptedRequest, msg: InterceptedMessage
    ) -> InterceptedRequest:
        """Apply message modifications back to InterceptedRequest."""
        if msg.body is not original.body:
            original.body = msg.body
        if msg.path != original.path:
            original.path = msg.path
        # Only rewrite headers when a rule actually changed them (the REDIRECT
        # action sets ``host``). In all other cases keep the intercepted keys
        # as-is: re-casing every header (e.g. ``content-type`` -> ``Content-Type``)
        # on every request broke downstream adapters that expect lowercase keys.
        if msg.headers != {k.lower(): v for k, v in original.headers.items()}:
            original.headers = dict(msg.headers)
        original.metadata = msg.metadata
        original.modifications = msg.modifications
        if msg.blocked:
            original.blocked = True
            original.block_reason = msg.block_reason
        return original

    def _apply_to_response(
        self, original: dict[str, Any], msg: InterceptedMessage
    ) -> dict[str, Any]:
        """Apply message modifications back to response dict."""
        if msg.body is not None:
            # Merge the modified body into the original response shape instead
            # of replacing the dict wholesale, so status/headers set by the
            # adapter survive the round-trip.
            if isinstance(msg.body, dict) and isinstance(original, dict):
                orig_body = original.get("body")
                if isinstance(orig_body, dict):
                    merged = dict(orig_body)
                    merged.update(msg.body)
                    original = dict(original)
                    original["body"] = merged
                else:
                    original = dict(original)
                    original["body"] = msg.body
            else:
                original = msg.body
        if msg.modifications:
            original = dict(original)
            original["modifications"] = msg.modifications
        if msg.blocked:
            original = {"success": False, "error": msg.block_reason}
        return original

    def _log_modification(
        self,
        rule: ModificationRule,
        msg: InterceptedMessage,
        original_body: Any,  # noqa: ANN401
        _original_headers: dict | None,
    ):
        """Log modification to audit trail."""
        orig_body_str = (
            original_body
            if isinstance(original_body, str)
            else (json.dumps(original_body) if original_body is not None else None)
        )
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "rule": rule.name,
            "action": rule.action.value,
            "device_id": msg.device_id,
            "vendor": msg.vendor,
            "direction": msg.direction,
            "original_body": orig_body_str,
            "modified_body": json.dumps(msg.body) if msg.body is not None else None,
            "modifications": msg.modifications[-1] if msg.modifications else None,
        }

        self._audit_log.append(entry)
        if len(self._audit_log) > self._max_audit:
            self._audit_log = self._audit_log[-self._max_audit :]

        # Also write to file if configured
        config = self.config_manager.config
        mod_config = getattr(config, "modification", None)
        if mod_config and getattr(mod_config, "audit_log_enabled", True):
            audit_path = getattr(mod_config, "audit_log_path", "./logs/modifications.jsonl")
            try:
                append_jsonl(audit_path, entry)
            except Exception as e:
                logger.error(f"Failed to write audit log: {e}")  # noqa: TRY400

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        """Get recent modification audit log."""
        return self._audit_log[-limit:]


# Global instance
_modification_engine: ModificationEngine | None = None


def get_modification_engine() -> ModificationEngine:
    """Get or create global modification engine instance."""
    global _modification_engine  # noqa: PLW0603
    if _modification_engine is None:
        _modification_engine = ModificationEngine()
    return _modification_engine
