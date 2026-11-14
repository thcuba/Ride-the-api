"""Unit tests verifying the modularized core.llm architecture and backward compatibility."""

from __future__ import annotations

import pytest

import core.llm as llm_pkg
import core.llm.client_pool as pool_mod
import core.llm.models as models_mod
import core.llm.parser as parser_mod
import core.llm.profiles as profiles_mod
import core.llm.prompts as prompts_mod
import core.llm.service as service_mod
import core.llm_decipher as shim_mod
from core.fallback_policy import ProviderCircuitBreaker
from core.llm import (
    LLMClientPool,
    LLMDecipherService,
    LLMProfile,
    build_params_prompt,
    build_validation_prompt,
    get_available_profiles,
    get_default_db_schema,
    order_profiles,
    validate_task_profiles,
)

TIMEOUT = 15


def test_llm_package_exports() -> None:
    """Verify core.llm and core.llm_decipher export all expected symbols."""
    expected_symbols = [
        "AsyncOpenAI",
        "DecipherResult",
        "LLMCallError",
        "LLMClientPool",
        "LLMDecipherAnalysis",
        "LLMDecipherService",
        "LLMProfile",
        "RequestResponsePair",
        "_is_retryable_llm_error",
        "_llm_decipher",
        "_parse_llm_json",
        "_raw_secret",
        "_strict_true",
        "build_decipher_prompt",
        "build_params_prompt",
        "build_validation_prompt",
        "execute_profile_chain",
        "get_available_profiles",
        "get_default_db_schema",
        "get_llm_decipher",
        "get_recent_patterns",
        "order_profiles",
        "validate_task_profiles",
    ]
    for symbol in expected_symbols:
        assert hasattr(shim_mod, symbol), f"core.llm_decipher missing symbol {symbol}"
        if symbol != "AsyncOpenAI":
            assert hasattr(llm_pkg, symbol), f"core.llm missing symbol {symbol}"


def test_direct_submodule_imports() -> None:
    """Verify symbols can be imported directly from focused submodules."""
    assert pool_mod.LLMClientPool is LLMClientPool
    assert models_mod.LLMProfile is LLMProfile
    assert parser_mod._parse_llm_json is llm_pkg._parse_llm_json
    assert prompts_mod.build_validation_prompt is build_validation_prompt
    assert profiles_mod.order_profiles is order_profiles
    assert service_mod.LLMDecipherService is LLMDecipherService


def test_client_pool_standalone() -> None:
    """Verify LLMClientPool manages fingerprints and client builds."""
    pool = LLMClientPool()
    profile = LLMProfile(
        name="test-prof",
        base_url="https://api.example.com/v1",
        api_key="secret123",
        model_id="gpt-4",
        prompt_template="{pairs}",
        timeout=TIMEOUT,
        max_retries=3,
    )
    fp = pool.client_fingerprint(profile)
    assert fp == ("https://api.example.com/v1", "secret123", TIMEOUT, 3)

    client = pool.build_client(profile)
    assert str(client.base_url).rstrip("/") == "https://api.example.com/v1"
    assert client.timeout == TIMEOUT

    # Cached client
    cached = pool.get_client(profile)
    assert pool.get_client(profile) is cached

    # Prune stale
    pool.prune_stale_clients({"test-prof": ("changed_url", "secret123", TIMEOUT, 3)})
    assert len(pool._clients) == 0


def test_prompts_standalone() -> None:
    """Verify prompt formatting helpers in core.llm.prompts."""
    val_prompt = build_validation_prompt(
        {"intent": "status", "method": "GET", "path": "/info"},
        {"method": "GET", "path": "/info", "headers": {}, "body": {}},
    )
    assert "LEARNED PATTERN:" in val_prompt
    assert "INCOMING REQUEST:" in val_prompt

    params_prompt = build_params_prompt(
        "Vendor {vendor} on device {device_id}",
        {"vendor": "Shelly", "device_id": "sh-01"},
    )
    assert params_prompt == "Vendor Shelly on device sh-01"

    schema = get_default_db_schema("Tuya")
    assert "Vendor: Tuya" in schema
    assert "intercepted_requests" in schema


def test_profiles_standalone() -> None:
    """Verify profile ordering and task validation in core.llm.profiles."""
    tasks = validate_task_profiles(
        {"correlation": "p1"},
        {"p1": {}},
        {"correlation": "desc", "flush": "desc"},
    )
    assert tasks == {"correlation": "p1"}

    with pytest.raises(ValueError, match="unknown task"):
        validate_task_profiles({"unknown": "p1"}, {"p1": {}}, {"correlation": "desc"})

    ordered = order_profiles(["p1", "p2", "p3"], preferred="p2", default_profile="p1")
    assert ordered == ["p2", "p1", "p3"]

    p1 = LLMProfile(
        name="p1",
        base_url="https://api.openai.com/v1",
        model_id="m1",
        prompt_template="",
    )
    breaker = ProviderCircuitBreaker()
    avail = get_available_profiles(
        {"p1": p1},
        breaker,
        enabled=True,
        preferred="p1",
        default_profile="p1",
    )
    assert avail == ["p1"]


@pytest.mark.asyncio
async def test_service_delegation() -> None:
    """Verify LLMDecipherService delegates to client pool and prompt modules."""
    svc = LLMDecipherService()
    assert hasattr(svc, "client_pool")
    assert isinstance(svc.client_pool, LLMClientPool)
    assert svc._clients is svc.client_pool._clients

    # Method delegation check
    assert svc._build_validation_prompt is build_validation_prompt
    await svc.close()


@pytest.mark.asyncio
async def test_service_builder_override_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify overriding svc._build_client is respected by svc._get_client."""
    svc = LLMDecipherService()
    profile = LLMProfile(
        name="override-prof",
        base_url="https://api.example.com/v1",
        api_key="secret",
        model_id="gpt-4",
        prompt_template="",
    )
    custom_sentinel = object()
    monkeypatch.setattr(svc, "_build_client", lambda _p: custom_sentinel)
    client = svc._get_client(profile)
    assert client is custom_sentinel
    await svc.close()
