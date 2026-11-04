"""Weak-match validation and LLM client hardening (S-2, C-19, S-10)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.llm_decipher import LLMDecipherService, LLMProfile, _strict_true
from tests.test_llm_decipher import _service_with_profiles


def test_validation_prompt_redacts_live_request_credentials():
    pattern = SimpleNamespace(
        intent="get_status", method="GET", path_pattern="/api/status", protocol="http"
    )
    request = {
        "method": "GET",
        "path": "/api/status",
        "headers": {"Authorization": "Bearer RAW-SECRET-1", "Cookie": "sid=RAW-SECRET-2"},
        "body": {"password": "RAW-SECRET-3", "mode": "eco"},
    }
    prompt = LLMDecipherService._build_validation_prompt(pattern, request)
    for secret in ("RAW-SECRET-1", "RAW-SECRET-2", "RAW-SECRET-3"):
        assert secret not in prompt
    # Non-secret context is still there for the model to judge the match.
    assert "eco" in prompt


def test_validation_prompt_uses_path_pattern_for_db_patterns():
    pattern = SimpleNamespace(intent="x", method="GET", path_pattern="/api/learned")
    prompt = LLMDecipherService._build_validation_prompt(
        pattern, {"method": "GET", "path": "/api/learned"}
    )
    assert '"path": "/api/learned"' in prompt.split("INCOMING REQUEST")[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        ("true", True),
        ("Yes", True),
        (False, False),
        ("false", False),
        ("no", False),
        ("", False),
        (1, False),
        (None, False),
    ],
)
def test_strict_true(value, expected):
    assert _strict_true(value) is expected


@patch("core.llm_decipher.AsyncOpenAI")
def test_override_profile_gets_its_own_client(mock_client_class):
    """A per-device override reusing a profile name must not share its client."""
    service, _cm = _service_with_profiles()
    base = service._profiles["default"]
    override = LLMProfile(
        name=base.name,
        base_url="http://10.0.0.9:11434/v1",
        api_key=base.api_key,
        model_id="llama3",
        prompt_template=base.prompt_template,
    )

    service._get_client(override)
    service._get_client(base)
    # Two distinct connection fingerprints -> two clients built.
    assert mock_client_class.call_count == 2  # noqa: PLR2004
    built_urls = [call.kwargs["base_url"] for call in mock_client_class.call_args_list]
    assert built_urls == ["http://10.0.0.9:11434/v1", base.base_url.rstrip("/")]

    # The shared profile keeps getting its own cached client.
    service._get_client(base)
    assert mock_client_class.call_count == 2  # noqa: PLR2004
