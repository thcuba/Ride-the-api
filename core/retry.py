"""Shared retry/backoff helpers built on the validated ``tenacity`` library.

Centralizes the retry policy so LLM calls, cloud forwarding and protocol
connects all use the same exponential-backoff strategy instead of hand-rolled
``asyncio.sleep`` loops.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tenacity import (
    AsyncRetrying,
    Retrying,
    before_sleep_log,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Default policy shared by all async callers.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_WAIT = 1
DEFAULT_MAX_WAIT = 8


def wait_strategy(
    multiplier: float = 1.0,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> wait_exponential:
    """Exponential backoff with optional jitter (multiplier=1 -> 1,2,4,8...)."""
    return wait_exponential(multiplier=multiplier, min=min_wait, max=max_wait)


def make_retryer(  # noqa: PLR0913
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
    log_level: int = logging.WARNING,
    *,
    retry_if: Callable[[BaseException], bool] | None = None,
) -> AsyncRetrying:
    """Build a configured ``AsyncRetrying`` instance.

    Args:
        max_attempts: Total attempts including the first (1 = no retries).
        retry_on: Exception types that trigger a retry.
        min_wait: Minimum sleep seconds between attempts.
        max_wait: Maximum sleep seconds between attempts.
        log_level: Logging level for the before-sleep log hook.
        retry_if: Optional predicate; when given, an exception is retried only
            if it is one of ``retry_on`` *and* the predicate returns True (e.g.
            to give up at once on a 401 that no retry can fix).
    """
    retry = retry_if_exception_type(retry_on)
    if retry_if is not None:
        retry = retry & retry_if_exception(retry_if)
    return AsyncRetrying(
        stop=stop_after_attempt(max(max_attempts, 1)),
        wait=wait_strategy(min_wait=min_wait, max_wait=max_wait),
        retry=retry,
        before_sleep=before_sleep_log(logger, log_level),
        reraise=False,
    )


def make_sync_retryer(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Retrying:
    """Build a configured synchronous ``Retrying`` instance."""
    return Retrying(
        stop=stop_after_attempt(max(max_attempts, 1)),
        wait=wait_strategy(min_wait=min_wait, max_wait=max_wait),
        retry=retry_if_exception_type(retry_on),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=False,
    )
