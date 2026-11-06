"""LLM response parsing, json repair, and error handling helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

import json_repair
from openai import APIStatusError
from pydantic import SecretStr

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({408, 409, 429})
_SERVER_ERROR_MIN = 500


def _parse_llm_json(content: str) -> dict | None:
    """Parse JSON from an LLM response, tolerating surrounding markdown/code fences.

    LLMs often wrap the JSON payload in ```json ... ``` fences, add prose
    around it, or emit slightly malformed JSON (trailing commas, truncation).
    ``json_repair`` robustly extracts and repairs such payloads; a plain
    ``json.loads`` still handles clean output fast. Returns the parsed dict,
    or ``None`` when nothing recoverable is present.
    """
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        try:
            return json_repair.loads(content)
        except Exception:
            return None


def _raw_secret(value: object) -> str:
    """A configured secret as a plain string (unwrapping ``SecretStr``)."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value if isinstance(value, str) else ""


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """Whether an LLM call failure is worth another attempt."""
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS or exc.status_code >= _SERVER_ERROR_MIN
    return True


_TRUE_STRINGS = frozenset({"true", "yes"})


def _strict_true(value: Any) -> bool:
    """Read an LLM-supplied yes/no verdict without trusting Python truthiness.

    ``bool("false")`` is True, so a model that quotes its boolean would have
    every weak match served locally. Only a real ``True`` or the strings
    ``"true"``/``"yes"`` (any case) count as yes; everything else is no.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return False
