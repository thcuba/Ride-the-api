"""
Fallback-policy layer — structured per-request fallback chains and circuit breaking.

Two cooperating pieces:

1. :class:`ProviderCircuitBreaker` — tracks per-provider (LLM profile) health.
   After ``failure_threshold`` consecutive failures a provider is *open*
   (skipped) for ``cooldown_seconds``, then *half-open* (one probe allowed)
   before being closed again on success. This replaces any ad-hoc
   "try the first provider and give up" logic with a deterministic rotation,
   so a degraded LLM provider never forces analysis to fail outright when a
   healthy alternative profile exists.

2. :class:`FallbackStep` / :class:`FallbackChain` — run an ordered list of
   handlers (learned-pattern match -> cloud forward -> safe local default)
   and record which step served the request. The chain never drops a request:
   the final step is a safe default that always produces a response.

The design goal is *availability with no data loss*: prefer a safe local
default over dropping a request, and let a degraded provider fall through to a
healthy one before disabling the whole path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Circuit-breaker default tuning.
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


class ProviderCircuitBreaker:
    """Per-provider circuit breaker with closed / open / half-open states.

    Intended for LLM provider profiles: a provider that repeatedly fails is
    temporarily excluded from rotation, then probed after a cooldown. A single
    success closes it again.
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._half_open: dict[str, bool] = {}

    # -- recording ---------------------------------------------------------

    def record_success(self, name: str) -> None:
        """Close the circuit and reset failure counters for ``name``."""
        self._failures.pop(name, None)
        self._opened_at.pop(name, None)
        self._half_open.pop(name, None)

    def record_failure(self, name: str) -> None:
        """Increment the failure counter; trip the circuit when exceeded."""
        failures = self._failures.get(name, 0) + 1
        self._failures[name] = failures
        if failures >= self.failure_threshold:
            # If we were half-open (a probe failed), re-open the circuit.
            self._opened_at[name] = time.monotonic()
            self._half_open[name] = False
            logger.warning(
                "Circuit opened for provider %r after %d consecutive failures",
                name,
                failures,
            )

    # -- status ------------------------------------------------------------

    def is_open(self, name: str) -> bool:
        """Return True while the provider is excluded (not allowing calls)."""
        if self._half_open.get(name):
            # A probe is currently allowed; a subsequent failure re-opens.
            return False
        opened_at = self._opened_at.get(name)
        if opened_at is None:
            return False
        if time.monotonic() - opened_at >= self.cooldown_seconds:
            # Cooldown elapsed -> move to half-open (allow one probe).
            self._half_open[name] = True
            return False
        return True

    def is_closed(self, name: str) -> bool:
        """True when the provider is fully healthy (no failures outstanding)."""
        return name not in self._failures and name not in self._opened_at

    def allow(self, name: str) -> bool:
        """Whether a call to ``name`` is permitted right now."""
        return not self.is_open(name)

    def state(self, name: str) -> str:
        """Return ``"closed"``, ``"open"`` or ``"half_open"`` for ``name``."""
        if self._half_open.get(name):
            return "half_open"
        if self.is_open(name):
            return "open"
        return "closed"

    def healthy_names(self, names: list[str]) -> list[str]:
        """Return the providers in ``names`` that currently allow calls."""
        return [n for n in names if self.allow(n)]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a diagnostic snapshot of every tracked provider."""
        names = set(self._failures) | set(self._opened_at) | set(self._half_open)
        return {
            n: {
                "state": self.state(n),
                "failures": self._failures.get(n, 0),
            }
            for n in sorted(names)
        }


@dataclass
class FallbackOutcome:
    """Result of executing a fallback chain."""

    step: str
    value: Any = None
    error: str | None = None
    history: list[str] = field(default_factory=list)


FallbackHandler = Callable[[], Awaitable[tuple[bool, Any]]]


class FallbackChain:
    """Run ordered handlers and return the first that succeeds.

    Each handler is an async callable returning ``(success, value)``. Handlers
    that fail (return ``success=False`` or raise) are skipped, their names are
    recorded, and the next step in the chain is attempted. If every step fails,
    the outcome reflects the last attempted step (and its error).
    """

    def __init__(self, steps: dict[str, FallbackHandler]) -> None:
        self.steps = steps

    async def run(self, *, strict: bool = False) -> FallbackOutcome:
        """Execute the chain.

        Args:
            strict: If True, raise when *every* step fails (used by callers that
                must not mask an error). Default False: return the last failure.
        """
        history: list[str] = []
        last_error: str | None = None

        for name, handler in self.steps.items():
            try:
                success, value = await handler()
                history.append(f"{name}:ok" if success else f"{name}:fail")
                if success:
                    return FallbackOutcome(step=name, value=value, history=history)
                detail = value if isinstance(value, str) else "handler returned failure"
                last_error = f"{name}: {detail}"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{name}: {exc}"
                history.append(f"{name}:error")
                logger.warning("Fallback step %s failed: %s", name, exc)

        if strict:
            raise RuntimeError(f"All fallback steps failed: {last_error}")

        last_step = next(reversed(self.steps.keys()))
        return FallbackOutcome(
            step=last_step,
            error=last_error,
            history=history,
        )
