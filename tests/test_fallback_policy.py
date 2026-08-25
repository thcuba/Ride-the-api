"""
Tests for the fallback-policy layer: circuit breaker and fallback chains.
"""

from unittest.mock import patch

import pytest

from core.fallback_policy import FallbackChain, ProviderCircuitBreaker
from core.llm_decipher import LLMDecipherService, LLMProfile
from tests.test_llm_decipher import MockConfigManager


def make_breaker(threshold=3, cooldown=60.0):
    return ProviderCircuitBreaker(
        failure_threshold=threshold,
        cooldown_seconds=cooldown,
    )


class TestProviderCircuitBreaker:
    def test_initial_state_closed(self):
        breaker = make_breaker()
        assert breaker.state("provider-a") == "closed"
        assert breaker.allow("provider-a") is True
        assert breaker.is_closed("provider-a") is True
        assert breaker.is_open("provider-a") is False

    def test_opens_after_threshold_failures(self):
        breaker = make_breaker(threshold=3, cooldown=3600.0)
        for _ in range(2):
            breaker.record_failure("provider-a")
        assert breaker.state("provider-a") == "closed"
        breaker.record_failure("provider-a")  # 3rd -> open
        assert breaker.state("provider-a") == "open"
        assert breaker.allow("provider-a") is False

    def test_success_resets_failures(self):
        breaker = make_breaker(threshold=2, cooldown=3600.0)
        breaker.record_failure("provider-a")
        breaker.record_success("provider-a")
        assert breaker.state("provider-a") == "closed"
        breaker.record_failure("provider-a")
        assert breaker.state("provider-a") == "closed"  # only 1 failure again

    def test_half_open_after_cooldown(self):
        breaker = make_breaker(threshold=1, cooldown=0.05)
        breaker.record_failure("provider-a")
        assert breaker.state("provider-a") == "open"
        # Backdate the open time so the cooldown has elapsed.
        breaker._opened_at["provider-a"] -= 1.0
        # A status check transitions to half-open and allows one probe.
        assert breaker.allow("provider-a") is True
        assert breaker.state("provider-a") == "half_open"

    def test_half_open_probe_failure_reopens(self):
        breaker = make_breaker(threshold=1, cooldown=0.05)
        # Open it (record_failure calls monotonic once).
        with patch("core.fallback_policy.time.monotonic") as mono:
            mono.return_value = 0.0
            breaker.record_failure("provider-a")
        # Advance past cooldown -> half-open.
        with patch("core.fallback_policy.time.monotonic") as mono:
            mono.return_value = 100.0
            assert breaker.is_open("provider-a") is False
            assert breaker.state("provider-a") == "half_open"
            breaker.record_failure("provider-a")  # probe fails -> re-open
        with patch("core.fallback_policy.time.monotonic") as mono:
            mono.return_value = 100.0
            assert breaker.state("provider-a") == "open"

    def test_healthy_names_and_snapshot(self):
        breaker = make_breaker(threshold=1, cooldown=3600.0)
        breaker.record_failure("provider-b")
        assert breaker.healthy_names(["provider-a", "provider-b"]) == ["provider-a"]
        snapshot = breaker.snapshot()
        assert snapshot["provider-b"]["state"] == "open"
        assert snapshot["provider-b"]["failures"] == 1  # noqa: PLR2004

    def test_threshold_floor(self):
        breaker = make_breaker(threshold=0)  # clamped to 1
        assert breaker.failure_threshold == 1  # noqa: PLR2004


class TestFallbackChain:
    async def _handler(self, success, value=None):
        return success, value

    async def test_first_success_wins(self):
        chain = FallbackChain(
            {
                "learned": lambda: self._handler(True, "local"),
                "cloud": lambda: self._handler(True, "cloud"),
            }
        )
        outcome = await chain.run()
        assert outcome.step == "learned"
        assert outcome.value == "local"
        assert outcome.history == ["learned:ok"]

    async def test_skips_failed_steps(self):
        chain = FallbackChain(
            {
                "learned": lambda: self._handler(False),
                "cloud": lambda: self._handler(True, "cloud"),
            }
        )
        outcome = await chain.run()
        assert outcome.step == "cloud"
        assert outcome.value == "cloud"
        assert outcome.history == ["learned:fail", "cloud:ok"]

    async def test_first_step_raises_continues(self):
        async def boom():
            raise RuntimeError("boom")

        chain = FallbackChain(
            {"learned": boom, "cloud": lambda: self._handler(True, "cloud")}
        )
        outcome = await chain.run()
        assert outcome.step == "cloud"
        assert outcome.value == "cloud"
        assert outcome.history == ["learned:error", "cloud:ok"]

    async def test_all_fail_returns_last_step_error(self):
        async def fail_with_error():
            return False, "nope"

        chain = FallbackChain({"a": fail_with_error, "b": fail_with_error})
        outcome = await chain.run()
        assert outcome.error is not None
        assert outcome.value is None
        # Falls back to the last step as a safe default.
        assert outcome.step == "b"
        assert outcome.history == ["a:fail", "b:fail"]

    async def test_strict_raises_on_all_fail(self):
        async def fail_with_error():
            return False, "nope"

        chain = FallbackChain({"a": fail_with_error, "b": fail_with_error})
        with pytest.raises(RuntimeError):
            await chain.run(strict=True)


class TestLLMServiceProfileRotation:
    def _make_service(self, profile_names=("primary", "secondary")):
        service = LLMDecipherService(config_manager=MockConfigManager())
        for i, name in enumerate(profile_names):
            service._profiles[name] = LLMProfile(
                name=name,
                base_url=f"https://provider{i}.example.com/v1",
                api_key="sk-test",
                model_id="model",
                prompt_template="Analyze: {pairs}",
            )
        return service

    def test_available_profiles_prefers_requested(self):
        service = self._make_service()
        names = service.available_profiles(preferred="primary")
        assert names[0] == "primary"
        assert names == ["primary", "secondary"]

    def test_available_profiles_excludes_open(self):
        service = self._make_service()
        # Trip the circuit on the preferred profile so it stays open.
        with patch("core.fallback_policy.time.monotonic") as mono:
            mono.return_value = 0.0
            for _ in range(3):
                service.profile_breaker.record_failure("primary")
        with patch("core.fallback_policy.time.monotonic") as mono:
            mono.return_value = 0.0  # still inside cooldown
            names = service.available_profiles(preferred="primary")
        assert "primary" not in names
        assert names == ["secondary"]

    def test_available_profiles_all_open_falls_back_to_first(self):
        service = self._make_service()
        for name in ("primary", "secondary"):
            for _ in range(3):
                service.profile_breaker.record_failure(name)
        with patch("core.fallback_policy.time.monotonic") as mono:
            mono.return_value = 0.0  # both open, still cooling down
            names = service.available_profiles(preferred="primary")
        # All are open, so the ordered list is returned anyway (no empty result).
        assert names == ["primary", "secondary"]

    async def test_call_profile_chain_falls_back_to_second(self):
        service = self._make_service()

        async def fake_call_llm(profile, _prompt):
            if profile.name == "primary":
                return {"success": False, "error": "down", "raw": ""}
            return {"success": True, "content": "fixture"}

        with patch.object(service, "call_llm", side_effect=fake_call_llm):
            result = await service.call_profile_chain(prompt="analyze this")

        assert result["success"] is True
        assert result["content"] == "fixture"
        assert result["profile"] == "secondary"

    async def test_call_profile_chain_all_fail(self):
        service = self._make_service()

        async def fake_call_llm(_profile, _prompt):
            return {"success": False, "error": "down", "raw": ""}

        with patch.object(service, "call_llm", side_effect=fake_call_llm):
            result = await service.call_profile_chain(prompt="analyze this")

        assert result["success"] is False
        assert "error" in result
        assert result["error"]
