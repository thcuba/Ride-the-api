"""Pydantic schemas and dataclasses for LLM profiles, requests, and extraction results."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator


class LLMProfile(BaseModel):
    """Configuration for an LLM provider."""

    name: str
    base_url: str
    api_key: SecretStr = SecretStr("")
    model_id: str
    prompt_template: str
    enabled: bool = True
    timeout: int = 30
    max_retries: int = 2
    max_tokens: int = 2048

    @field_validator("api_key", mode="before")
    @classmethod
    def _resolve_env_var(cls, v: object) -> object:
        """Normalize the API key before it is coerced to ``SecretStr``.

        Accepts a plain string, an already-wrapped ``SecretStr`` (both are used
        by callers), or ``None``; resolves a ``${ENV_VAR}`` reference from the
        environment. Anything else becomes an empty string.

        Handling ``SecretStr`` matters: the previous version only accepted
        ``str``, so callers passing an already-wrapped key (the profile-copy
        path and the connection test) had it silently blanked, and the SDK then
        received the ``"not-set"`` placeholder — which made those LLM paths fail
        authentication no matter what the operator configured.
        """
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            return os.environ.get(v[2:-1], "")
        return v if isinstance(v, str) else ""


class LLMDecipherAnalysis(BaseModel):
    """Typed, tolerant view of an unstructured LLM analysis response.

    The LLM is instructed to output structured JSON, but it may omit fields,
    add extra keys, or emit noisy values (e.g. ``confidence: "high"``). Every
    field defaults so a partially malformed payload never aborts deciphering,
    unknown keys are ignored, and ``confidence`` stays untyped to be coerced
    through :func:`_safe_float` at the point of use.
    """

    intent: str = "unknown"
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: Any = 0.0
    suggested_dp_codes: dict[str, Any] = Field(default_factory=dict)
    protocol_notes: str = ""
    model_config = {"extra": "ignore"}


class LLMCallError(Exception):
    """Raised when an LLM call returns an unusable response."""


@dataclass
class DecipherResult:
    """Result of LLM deciphering."""

    pair_id: str
    device_id: str
    vendor: str
    intent: str
    fields: dict[str, Any]
    confidence: float
    suggested_dp_codes: dict[str, Any] = field(default_factory=dict)
    protocol_notes: str = ""
    raw_response: str = ""
    success: bool = True
    error: str | None = None
    processing_time_ms: float = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class RequestResponsePair:
    """A correlated request/response pair captured from device traffic.

    Used as the input to the LLM deciphering methods. ``request`` and
    ``response`` are lightweight dict-like payloads produced by the
    correlation layer (kept non-typed to stay agnostic of protocol).
    """

    pair_id: str
    device_id: str
    vendor: str
    protocol: str
    request: Any
    response: Any
