"""Prompt engineering and context preparation for LLM deciphering and validation."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from core.redaction import redact_body, redact_headers

if TYPE_CHECKING:
    from core.llm.models import LLMProfile, RequestResponsePair

logger = logging.getLogger(__name__)


def build_validation_prompt(pattern: Any, request: dict) -> str:  # noqa: ANN401
    """Prompt asking the LLM to confirm a weak pattern match."""

    def _get(obj: Any, name: str, default: Any = None) -> Any:  # noqa: ANN401
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    learned = {
        "intent": _get(pattern, "intent"),
        "method": _get(pattern, "method"),
        # DB-backed patterns store the path as ``path_pattern``.
        "path": _get(pattern, "path") or _get(pattern, "path_pattern"),
        "protocol": _get(pattern, "protocol"),
        "response_template": _get(pattern, "response_template"),
    }
    # Redact credentials before sending to LLM
    incoming = {
        "method": request.get("method"),
        "path": request.get("path"),
        "headers": redact_headers(request.get("headers")),
        "body": redact_body(request.get("body")),
    }
    return (
        "You validate a weak pattern match in a cloud-replacement proxy.\n"
        "A pattern already learned for this device is given below, together with\n"
        "an incoming request. Decide whether the learned pattern applies to the\n"
        "incoming request (same endpoint and shape), so the proxy can answer it\n"
        "locally instead of forwarding it to the cloud.\n\n"
        f"LEARNED PATTERN:\n{json.dumps(learned, default=str)[:4000]}\n\n"
        f"INCOMING REQUEST:\n{json.dumps(incoming, default=str)[:4000]}\n\n"
        "Answer with ONLY a JSON object: "
        '{"applicable": true|false, "confidence": 0.0-1.0, "reason": "<short>"}'
    )


def build_decipher_prompt(
    profile: LLMProfile,
    pair: RequestResponsePair,
    db_schema: str,
    recent_patterns: list[dict],
) -> str:
    """Build the analysis prompt for a request/response pair."""
    req = pair.request
    resp = pair.response

    pairs_text = f"""
Request:
  Method: {req.method}
  Path: {req.path}
  Headers: {json.dumps(req.headers, indent=2)}
  Query Params: {json.dumps(req.query_params, indent=2)}
  Body: {json.dumps(req.body, indent=2) if req.body else "empty"}

Response:
  Status: {resp.status_code}
  Headers: {json.dumps(resp.headers, indent=2)}
  Body: {json.dumps(resp.body, indent=2) if resp.body else "empty"}
  Latency: {resp.latency_ms:.1f}ms
"""

    prompt = profile.prompt_template
    request_meta = getattr(pair.request, "metadata", {}) or {}
    replacements = {
        "{vendor}": pair.vendor,
        "{device_type}": request_meta.get("device_type", "unknown"),
        "{db_schema}": db_schema,
        "{recent_patterns}": json.dumps(recent_patterns, indent=2),
        "{pairs}": pairs_text,
    }
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt


def build_params_prompt(template: str, context: dict) -> str:
    """Interpolate context dictionary parameters into a prompt template."""
    pairs_text = json.dumps(context.get("pairs", []), indent=2, default=str)
    prompt = template
    replacements = {
        "{vendor}": context.get("vendor", "unknown"),
        "{device_type}": context.get("device_type", "unknown"),
        "{pairs}": pairs_text,
        "{device_id}": context.get("device_id", "unknown"),
    }
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt


def get_default_db_schema(vendor: str) -> str:
    """Get default database schema reference for prompt grounding."""
    return f"""
Vendor: {vendor}
Tables:
- devices (device_id, name, type, capabilities, config)
- readings (device_id, timestamp, temp_target, temp_actual, humidity, power_watts, mode, fan_speed)
- commands (device_id, timestamp, command_type, params, success, response)
- models (vendor, device_type, model_path, accuracy, version)
- policies (vendor, device_type, rules_json)
- intercepted_requests (device_id, protocol, method, path, body, parsed_intent, parsed_params)
"""


def get_recent_patterns(
    loader: Any,  # noqa: ANN401
    vendor: str,
    device_type: str | None,
) -> list[dict]:
    """Retrieve recent patterns using an optional registered loader."""
    if loader is None:
        return []
    try:
        patterns = loader(vendor, device_type)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load recent patterns for %s", vendor)
        return []
    return patterns if isinstance(patterns, list) else []


__all__ = [
    "build_decipher_prompt",
    "build_params_prompt",
    "build_validation_prompt",
    "get_default_db_schema",
    "get_recent_patterns",
]
