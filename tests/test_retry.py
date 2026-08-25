"""Tests for the shared tenacity-based retry helpers."""

import asyncio

import pytest

from core.retry import make_retryer, make_sync_retryer


class TestMakeRetryer:
    @pytest.mark.asyncio
    async def test_retries_until_success(self):
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
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
        assert calls == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self):
        from tenacity import RetryError

        calls = 0

        async def always_fails():
            nonlocal calls
            calls += 1
            raise ConnectionError("boom")

        retryer = make_retryer(max_attempts=3, retry_on=(ConnectionError,), min_wait=0.01)
        with pytest.raises(RetryError):
            async for attempt in retryer:
                with attempt:
                    await always_fails()
        assert calls == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_no_retry_when_exception_not_in_retry_on(self):
        calls = 0

        async def raises_value():
            nonlocal calls
            calls += 1
            raise ValueError("not retried")

        retryer = make_retryer(max_attempts=3, retry_on=(ConnectionError,), min_wait=0.01)
        with pytest.raises(ValueError):
            async for attempt in retryer:
                with attempt:
                    await raises_value()
        assert calls == 1  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_single_attempt_is_no_retry(self):
        from tenacity import RetryError

        calls = 0

        async def fails():
            nonlocal calls
            calls += 1
            raise RuntimeError("x")

        retryer = make_retryer(max_attempts=1, retry_on=(RuntimeError,), min_wait=0.01)
        with pytest.raises(RetryError):
            async for attempt in retryer:
                with attempt:
                    await fails()
        assert calls == 1  # noqa: PLR2004

    def test_make_sync_retryer_config(self):
        retryer = make_sync_retryer(max_attempts=4)
        assert retryer.stop.max_attempt_number == 4  # noqa: PLR2004
