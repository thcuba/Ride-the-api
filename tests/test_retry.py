"""Tests for the shared tenacity-based retry helpers."""

import asyncio

import pytest
from tenacity import RetryError

from core.retry import make_retryer, make_sync_retryer


async def _exhaust(retryer, target) -> None:
    """Drive ``retryer`` attempts, running ``target()`` inside each attempt.

    Used by tests that expect a terminal exception to propagate out of
    tenacity's async retry generator.
    """
    async for attempt in retryer:
        with attempt:
            await target()


class TestMakeRetryer:
    @pytest.mark.asyncio
    async def test_retries_until_success(self):
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:  # noqa: PLR2004
                raise ConnectionError("boom")
            return "ok"

        retryer = make_retryer(max_attempts=5, retry_on=(ConnectionError,))
        try:
            async for attempt in retryer:
                with attempt:
                    result = flaky()
                    result = await result if asyncio.iscoroutine(result) else result
                    return  # unreachable
        except Exception:
            pass
        assert calls == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self):
        calls = 0

        async def always_fails():
            nonlocal calls
            calls += 1
            raise ConnectionError("boom")

        retryer = make_retryer(max_attempts=3, retry_on=(ConnectionError,), min_wait=0.01)
        with pytest.raises(RetryError):
            await _exhaust(retryer, always_fails)
        assert calls == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_no_retry_when_exception_not_in_retry_on(self):
        calls = 0

        async def raises_value():
            nonlocal calls
            calls += 1
            raise ValueError("not retried")

        retryer = make_retryer(max_attempts=3, retry_on=(ConnectionError,), min_wait=0.01)
        with pytest.raises(ValueError, match="not retried"):
            await _exhaust(retryer, raises_value)
        assert calls == 1  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_single_attempt_is_no_retry(self):
        calls = 0

        async def fails():
            nonlocal calls
            calls += 1
            raise RuntimeError("x")

        retryer = make_retryer(max_attempts=1, retry_on=(RuntimeError,), min_wait=0.01)
        with pytest.raises(RetryError):
            await _exhaust(retryer, fails)
        assert calls == 1  # noqa: PLR2004

    def test_make_sync_retryer_config(self):
        retryer = make_sync_retryer(max_attempts=4)
        assert retryer.stop.max_attempt_number == 4  # noqa: PLR2004
