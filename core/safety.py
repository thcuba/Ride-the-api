"""
Hard Safety Layer - Pre-inference safety checks that CANNOT be overridden by AI models.
These rules are ALWAYS enforced before any command is sent to a device.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from adapters.base import Command, CommandType, DeviceCapability, DeviceState

logger = logging.getLogger(__name__)


class SafetyViolationType(str, Enum):
    """Types of safety violations."""
    TEMP_OUT_OF_RANGE = "temp_out_of_range"
    TEMP_CHANGE_TOO_FAST = "temp_change_too_fast"
    HUMIDITY_OUT_OF_RANGE = "humidity_out_of_range"
    POWER_EXCEEDS_LIMIT = "power_exceeds_limit"
    INVALID_MODE_TRANSITION = "invalid_mode_transition"
    INVALID_FAN_SPEED = "invalid_fan_speed"
    COMMUNICATION_LOSS = "communication_loss"
    DEVICE_OFFLINE = "device_offline"
    FIRMWARE_UPDATE_PENDING = "firmware_update_pending"
    MAINTENANCE_MODE = "maintenance_mode"
    EMERGENCY_STOP = "emergency_stop"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CONFLICTING_COMMANDS = "conflicting_commands"


class SafetyAction(str, Enum):
    """Action to take on safety violation."""
    BLOCK = "block"           # Block command entirely
    MODIFY = "modify"         # Modify command to safe values
    WARN = "warn"             # Allow but log warning
    FALLBACK = "fallback"     # Fallback to cloud


@dataclass
class SafetyViolation:
    """A safety rule violation."""
    violation_type: SafetyViolationType
    message: str
    action: SafetyAction
    severity: str = "highmedium  # low, medium, high, critical
    details: dict[str, Any] = field(default_factory=dict)
    suggested_fix: dict[str, Any] | None = None


@dataclass
class SafetyCheckResult:
    """Result of safety checks."""
    allowed: bool
    violations: list[SafetyViolation] = field(default_factory=list)
    modified_command: Command | None = None
    fallback_to_cloud: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# BASE SAFETY RULE
# ═══════════════════════════════════════════════════════════════════════════════

class SafetyRule(ABC):
    """Abstract base for safety rules."""
    
    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
    
    @abstractmethod
    async def check(
        self,
        command: Command,
        device_state: DeviceState | None,
        device_info: Any = None,  # DeviceInfo from adapters.base
        context: dict[str, Any] | None = None,
    ) -> list[SafetyViolation]:
        """Check if command violates this rule."""
        pass
    
    def is_applicable(self, command: Command, device_info: Any = None) -> bool:
        """Check if rule applies to this command/device."""
        return self.enabled


# ═══════════════════════════════════════════════════════════════════════════════
# TEMPERATURE SAFETY RULES
# ═══════════════════════════════════════════════════════════════════════════════

class TemperatureRangeRule(SafetyRule):
    """Enforce absolute temperature limits."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        if command.command_type != CommandType.SET_TEMPERATURE:
            return violations
        
        target_temp = command.params.get("temperature") or command.params.get("temp_set")
        if target_temp is None:
            return violations
        
        # Get limits from config or device capabilities
        min_temp = self.config.get("min_temp_c", 16.0)
        max_temp = self.config.get("max_temp_c", 30.0)
        
        # Override with device-specific if available
        if device_info and hasattr(device_info, 'capabilities'):
            caps = device_info.capabilities
            if isinstance(caps, dict):
                min_temp = caps.get("min_temp_c", min_temp)
                max_temp = caps.get("max_temp_c", max_temp)
        
        if target_temp < min_temp:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.TEMP_OUT_OF_RANGE,
                message=f"Target temperature {target_temp}°C below minimum {min_temp}°C",
                action=SafetyAction.MODIFY,
                severity="high",
                details={"target": target_temp, "min_allowed": min_temp},
                suggested_fix={"temperature": min_temp},
            ))
        
        if target_temp > max_temp:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.TEMP_OUT_OF_RANGE,
                message=f"Target temperature {target_temp}°C above maximum {max_temp}°C",
                action=SafetyAction.MODIFY,
                severity="high",
                details={"target": target_temp, "max_allowed": max_temp},
                suggested_fix={"temperature": max_temp},
            ))
        
        return violations


class TemperatureRateLimitRule(SafetyRule):
    """Limit rate of temperature change per hour."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        if command.command_type != CommandType.SET_TEMPERATURE:
            return violations
        
        if not device_state or device_state.temp_actual is None:
            return violations
        
        target_temp = command.params.get("temperature") or command.params.get("temp_set")
        if target_temp is None:
            return violations
        
        current_temp = device_state.temp_actual
        delta = abs(target_temp - current_temp)
        
        max_delta_per_hour = self.config.get("max_delta_per_hour", 3.0)
        
        # Check recent commands for cumulative change
        recent_changes = context.get("recent_temp_changes", []) if context else []
        recent_delta = sum(recent_changes) if recent_changes else 0
        total_delta = delta + recent_delta
        
        if total_delta > max_delta_per_hour:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.TEMP_CHANGE_TOO_FAST,
                message=f"Temperature change rate {total_delta:.1f}°C/hr exceeds limit {max_delta_per_hour}°C/hr",
                action=SafetyAction.MODIFY,
                severity="medium",
                details={
                    "current": current_temp,
                    "target": target_temp,
                    "delta": delta,
                    "recent_delta": recent_delta,
                    "total_delta": total_delta,
                    "max_allowed": max_delta_per_hour,
                },
                suggested_fix={"temperature": current_temp + (max_delta_per_hour if target_temp > current_temp else -max_delta_per_hour)},
            ))
        
        return violations


# ═══════════════════════════════════════════════════════════════════════════════
# POWER & ENERGY SAFETY RULES
# ═══════════════════════════════════════════════════════════════════════════════

class PowerLimitRule(SafetyRule):
    """Enforce maximum power consumption."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        # Only check if we can estimate power impact
        if command.command_type not in (CommandType.SET_TEMPERATURE, CommandType.TURN_ON, CommandType.SET_MODE, CommandType.SET_FAN_SPEED):
            return violations
        
        if not device_state or device_state.power_watts is None:
            return violations
        
        max_power = self.config.get("max_power_watts", 3500)
        current_power = device_state.power_watts
        
        # Estimate power after command (simplified)
        estimated_power = self._estimate_power(command, device_state, device_info)
        
        if estimated_power > max_power:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.POWER_EXCEEDS_LIMIT,
                message=f"Estimated power {estimated_power:.0f}W exceeds limit {max_power}W",
                action=SafetyAction.BLOCK,
                severity="critical",
                details={
                    "current_power": current_power,
                    "estimated_power": estimated_power,
                    "limit": max_power,
                },
            ))
        
        return violations
    
    def _estimate_power(self, command, device_state, device_info):
        """Simple power estimation based on command."""
        current = device_state.power_watts or 0
        
        if command.command_type == CommandType.TURN_ON:
            # Estimate based on mode and fan
            mode = device_state.mode or "cool"
            fan = device_state.fan_speed or "auto"
            return current * 1.5  # Rough estimate
        
        elif command.command_type == CommandType.SET_TEMPERATURE:
            target = command.params.get("temperature", 24)
            current_temp = device_state.temp_actual or 24
            delta = abs(target - current_temp)
            return current + (delta * 100)  # ~100W per degree
        
        elif command.command_type == CommandType.SET_FAN_SPEED:
            fan = command.params.get("fan_speed", "auto")
            fan_multiplier = {"low": 0.7, "medium": 1.0, "high": 1.3, "auto": 1.0}
            return current * fan_multiplier.get(fan, 1.0)
        
        return current


# ═══════════════════════════════════════════════════════════════════════════════
# MODE & STATE TRANSITION RULES
# ═══════════════════════════════════════════════════════════════════════════════

class ModeTransitionRule(SafetyRule):
    """Validate mode transitions are allowed."""
    
    # Allowed transitions: from -> list of allowed to
    ALLOWED_TRANSITIONS = {
        "off": ["cool", "heat", "fan", "auto", "dry"],
        "cool": ["off", "fan", "dry", "auto"],
        "heat": ["off", "fan", "auto"],
        "fan": ["off", "cool", "heat", "auto", "dry"],
        "auto": ["off", "cool", "heat", "fan", "dry"],
        "dry": ["off", "cool", "fan", "auto"],
    }
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        if command.command_type != CommandType.SET_MODE:
            return violations
        
        if not device_state or not device_state.mode:
            return violations
        
        current_mode = device_state.mode
        target_mode = command.params.get("mode")
        
        if not target_mode:
            return violations
        
        allowed = self.ALLOWED_TRANSITIONS.get(current_mode, [])
        if target_mode not in allowed:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.INVALID_MODE_TRANSITION,
                message=f"Mode transition {current_mode} -> {target_mode} not allowed",
                action=SafetyAction.BLOCK,
                severity="high",
                details={
                    "current_mode": current_mode,
                    "target_mode": target_mode,
                    "allowed_transitions": allowed,
                },
            ))
        
        return violations


class FanSpeedRule(SafetyRule):
    """Validate fan speed is supported by device."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        if command.command_type != CommandType.SET_FAN_SPEED:
            return violations
        
        fan_speed = command.params.get("fan_speed")
        if not fan_speed:
            return violations
        
        # Check device capabilities
        supported_speeds = ["low", "medium", "high", "auto"]
        
        if device_info and hasattr(device_info, 'capabilities'):
            caps = device_info.capabilities
            if isinstance(caps, dict):
                supported_speeds = caps.get("fan_speeds", supported_speeds)
        
        if fan_speed not in supported_speeds:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.INVALID_FAN_SPEED,
                message=f"Fan speed '{fan_speed}' not supported. Supported: {supported_speeds}",
                action=SafetyAction.BLOCK,
                severity="medium",
                details={"requested": fan_speed, "supported": supported_speeds},
            ))
        
        return violations


# ═══════════════════════════════════════════════════════════════════════════════
# COMMUNICATION & DEVICE HEALTH RULES
# ═══════════════════════════════════════════════════════════════════════════════

class CommunicationLossRule(SafetyRule):
    """Block commands if device hasn't communicated recently."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        if not device_state or not device_state.timestamp:
            return violations
        
        max_silence = self.config.get("max_silence_minutes", 15)
        silence_duration = (datetime.utcnow() - device_state.timestamp).total_seconds() / 60
        
        if silence_duration > max_silence:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.COMMUNICATION_LOSS,
                message=f"Device silent for {silence_duration:.0f} min (max {max_silence} min)",
                action=SafetyAction.BLOCK,
                severity="critical",
                details={
                    "last_seen": device_state.timestamp.isoformat(),
                    "silence_minutes": silence_duration,
                    "max_allowed": max_silence,
                },
            ))
        
        return violations


class DeviceOfflineRule(SafetyRule):
    """Block commands to offline devices."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        # Check device status from registry
        if device_info and hasattr(device_info, 'status'):
            if device_info.status == "offline":
                violations.append(SafetyViolation(
                    violation_type=SafetyViolationType.DEVICE_OFFLINE,
                    message="Device is offline",
                    action=SafetyAction.BLOCK,
                    severity="critical",
                    details={"status": device_info.status},
                ))
        
        return violations


class RateLimitRule(SafetyRule):
    """Rate limit commands per device."""
    
    def __init__(self, name: str, config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._command_counts: dict[str, list[datetime]] = {}
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        device_id = command.device_id
        max_per_minute = self.config.get("max_commands_per_minute", 10)
        max_per_hour = self.config.get("max_commands_per_hour", 100)
        
        now = datetime.utcnow()
        if device_id not in self._command_counts:
            self._command_counts[device_id] = []
        
        # Clean old entries
        self._command_counts[device_id] = [
            t for t in self._command_counts[device_id]
            if now - t < timedelta(hours=1)
        ]
        
        recent = self._command_counts[device_id]
        count_minute = sum(1 for t in recent if now - t < timedelta(minutes=1))
        count_hour = len(recent)
        
        if count_minute >= max_per_minute:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.RATE_LIMIT_EXCEEDED,
                message=f"Rate limit exceeded: {count_minute} commands/min (max {max_per_minute})",
                action=SafetyAction.BLOCK,
                severity="high",
                details={"count_minute": count_minute, "limit_minute": max_per_minute},
            ))
        
        if count_hour >= max_per_hour:
            violations.append(SafetyViolation(
                violation_type=SafetyViolationType.RATE_LIMIT_EXCEEDED,
                message=f"Hourly rate limit exceeded: {count_hour} commands/hr (max {max_per_hour})",
                action=SafetyAction.BLOCK,
                severity="medium",
                details={"count_hour": count_hour, "limit_hour": max_per_hour},
            ))
        
        return violations
    
    def record_command(self, device_id: str):
        """Record a command for rate limiting."""
        if device_id not in self._command_counts:
            self._command_counts[device_id] = []
        self._command_counts[device_id].append(datetime.utcnow())


# ═══════════════════════════════════════════════════════════════════════════════
# CONFLICT DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

class ConflictingCommandsRule(SafetyRule):
    """Detect conflicting commands in short timeframe."""
    
    CONFLICTING_PAIRS = [
        (CommandType.TURN_ON, CommandType.TURN_OFF),
        (CommandType.SET_MODE, CommandType.SET_MODE),  # Different modes
    ]
    
    def __init__(self, name: str, config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._recent_commands: dict[str, list[tuple[CommandType, datetime]]] = {}
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        device_id = command.device_id
        now = datetime.utcnow()
        window = timedelta(seconds=self.config.get("conflict_window_seconds", 30))
        
        if device_id not in self._recent_commands:
            self._recent_commands[device_id] = []
        
        # Clean old
        self._recent_commands[device_id] = [
            (cmd_type, ts) for cmd_type, ts in self._recent_commands[device_id]
            if now - ts < window
        ]
        
        # Check for conflicts
        for recent_type, ts in self._recent_commands[device_id]:
            for type_a, type_b in self.CONFLICTING_PAIRS:
                if ((command.command_type == type_a and recent_type == type_b) or
                    (command.command_type == type_b and recent_type == type_a)):
                    
                    # Special case for SET_MODE: check if different mode
                    if type_a == CommandType.SET_MODE and type_b == CommandType.SET_MODE:
                        # Would need to check params
                        pass
                    else:
                        violations.append(SafetyViolation(
                            violation_type=SafetyViolationType.CONFLICTING_COMMANDS,
                            message=f"Conflicting command: {command.command_type.value} after {recent_type.value} ({(now - ts).total_seconds():.0f}s ago)",
                            action=SafetyAction.WARN,
                            severity="medium",
                            details={
                                "current": command.command_type.value,
                                "recent": recent_type.value,
                                "seconds_ago": (now - ts).total_seconds(),
                            },
                        ))
        
        return violations
    
    def record_command(self, device_id: str, command_type: CommandType):
        """Record command for conflict detection."""
        if device_id not in self._recent_commands:
            self._recent_commands[device_id] = []
        self._recent_commands[device_id].append((command_type, datetime.utcnow()))


# ═══════════════════════════════════════════════════════════════════════════════
# VENDOR-SPECIFIC RULES
# ═══════════════════════════════════════════════════════════════════════════════

class TuyaSpecificRules(SafetyRule):
    """Tuya-specific safety rules."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        # Tuya devices often have specific DP code requirements
        if command.command_type == CommandType.SET_TEMPERATURE:
            target = command.params.get("temperature")
            if target is not None:
                # Tuya uses temp * 10
                if target < 160 or target > 300:  # 16.0 to 30.0
                    violations.append(SafetyViolation(
                        violation_type=SafetyViolationType.TEMP_OUT_OF_RANGE,
                        message=f"Tuya temperature {target/10}°C out of range (16-30°C)",
                        action=SafetyAction.MODIFY,
                        severity="high",
                        suggested_fix={"temperature": max(160, min(300, target))},
                    ))
        
        return violations


class ZehnderSpecificRules(SafetyRule):
    """Zehnder (ventilation) specific safety rules."""
    
    async def check(self, command, device_state, device_info, context):
        violations = []
        
        # Zehnder: don't turn off heat recovery in winter
        if command.command_type == CommandType.SET_MODE:
            mode = command.params.get("mode")
            if mode == "off" and device_state and device_state.temp_outdoor is not None:
                if device_state.temp_outdoor < 5.0:  # Below 5°C outside
                    violations.append(SafetyViolation(
                        violation_type=SafetyViolationType.MAINTENANCE_MODE,
                        message="Heat recovery should not be disabled below 5°C outdoor temp",
                        action=SafetyAction.WARN,
                        severity="medium",
                        details={"outdoor_temp": device_state.temp_outdoor},
                    ))
        
        return violations


# ═══════════════════════════════════════════════════════════════════════════════
# SAFETY ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class SafetyEngine:
    """
    Main safety engine that runs all safety rules before command execution.
    This is the LAST LINE OF DEFENSE - no AI model can bypass these rules.
    """
    
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.rules: list[SafetyRule] = []
        self.vendor_rules: dict[str, list[SafetyRule]] = {}
        self._initialized = False
    
    def initialize(self):
        """Initialize all safety rules."""
        if self._initialized:
            return
        
        # Global rules (apply to all vendors)
        self.rules = [
            TemperatureRangeRule("temp_range", self.config.get("temp_range", {})),
            TemperatureRateLimitRule("temp_rate_limit", self.config.get("temp_rate_limit", {})),
            PowerLimitRule("power_limit", self.config.get("power_limit", {})),
            ModeTransitionRule("mode_transition"),
            FanSpeedRule("fan_speed"),
            CommunicationLossRule("comm_loss", self.config.get("comm_loss", {})),
            DeviceOfflineRule("device_offline"),
            RateLimitRule("rate_limit", self.config.get("rate_limit", {})),
            ConflictingCommandsRule("conflicting_commands", self.config.get("conflicting", {})),
        ]
        
        # Vendor-specific rules
        self.vendor_rules = {
            "tuya": [TuyaSpecificRules("tuya_specific")],
            "zehnder": [ZehnderSpecificRules("zehnder_specific")],
        }
        
        self._initialized = True
        logger.info("Safety engine initialized with %d global rules", len(self.rules))
    
    async def check_command(
        self,
        command: Command,
        device_state: DeviceState | None,
        device_info: Any = None,
        context: dict[str, Any] | None = None,
    ) -> SafetyCheckResult:
        """
        Run all safety checks on a command.
        Returns SafetyCheckResult with allowed/modified/blocked decision.
        """
        if not self._initialized:
            self.initialize()
        
        all_violations: list[SafetyViolation] = []
        modified_params = dict(command.params)
        fallback_to_cloud = False
        
        # Get applicable rules
        vendor = getattr(device_info, 'vendor', None) if device_info else None
        applicable_rules = list(self.rules)
        if vendor and vendor in self.vendor_rules:
            applicable_rules.extend(self.vendor_rules[vendor])
        
        # Run all rules
        for rule in applicable_rules:
            if not rule.is_applicable(command, device_info):
                continue
            
            try:
                violations = await rule.check(command, device_state, device_info, context)
                all_violations.extend(violations)
            except Exception as e:
                logger.exception(f"Safety rule {rule.name} failed: {e}")
                # On rule failure, be safe: block
                all_violations.append(SafetyViolation(
                    violation_type=SafetyViolationType.EMERGENCY_STOP,
                    message=f"Safety rule {rule.name} error: {e}",
                    action=SafetyAction.BLOCK,
                    severity="critical",
                ))
        
        # Process violations
        for violation in all_violations:
            if violation.action == SafetyAction.BLOCK:
                return SafetyCheckResult(
                    allowed=False,
                    violations=all_violations,
                    fallback_to_cloud=False,
                )
            elif violation.action == SafetyAction.MODIFY:
                if violation.suggested_fix:
                    modified_params.update(violation.suggested_fix)
            elif violation.action == SafetyAction.FALLBACK:
                fallback_to_cloud = True
        
        # Create modified command if needed
        modified_command = None
        if modified_params != command.params:
            modified_command = Command(
                device_id=command.device_id,
                command_type=command.command_type,
                params=modified_params,
                source=command.source,
                edge_model_id=command.edge_model_id,
                confidence=command.confidence,
                correlation_id=command.correlation_id,
            )
        
        return SafetyCheckResult(
            allowed=True,
            violations=all_violations,
            modified_command=modified_command,
            fallback_to_cloud=fallback_to_cloud,
        )
    
    def record_command(self, device_id: str, command_type: CommandType):
        """Record command for rate limiting and conflict detection."""
        for rule in self.rules:
            if hasattr(rule, 'record_command'):
                rule.record_command(device_id, command_type)
    
    def get_safety_summary(self) -> dict[str, Any]:
        """Get summary of safety configuration."""
        return {
            "global_rules": [r.name for r in self.rules],
            "vendor_rules": {v: [r.name for r in rules] for v, rules in self.vendor_rules.items()},
            "config": self.config,
        }


# Default safety configuration
DEFAULT_SAFETY_CONFIG = {
    "temp_range": {
        "min_temp_c": 16.0,
        "max_temp_c": 30.0,
    },
    "temp_rate_limit": {
        "max_delta_per_hour": 3.0,
    },
    "power_limit": {
        "max_power_watts": 3500,
    },
    "comm_loss": {
        "max_silence_minutes": 15,
    },
    "rate_limit": {
        "max_commands_per_minute": 10,
        "max_commands_per_hour": 100,
    },
    "conflicting": {
        "conflict_window_seconds": 30,
    },
}