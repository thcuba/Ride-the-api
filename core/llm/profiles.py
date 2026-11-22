"""Profile resolution, task bindings, rotation ordering, and fallback chains."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from core.fallback_policy import FallbackChain, FallbackHandler

if TYPE_CHECKING:
    from core.fallback_policy import ProviderCircuitBreaker
    from core.llm.models import LLMProfile


def validate_task_profiles(
    raw_tasks: Any,  # noqa: ANN401
    validated: dict[str, dict],
    supported_tasks: dict[str, str],
) -> dict[str, str]:
    """Validate task -> profile bindings; an empty value means unbound."""
    if raw_tasks is None:
        raw_tasks = {}
    if not isinstance(raw_tasks, dict):
        raise ValueError("task_profiles must be an object")  # noqa: TRY004
    bindings: dict[str, str] = {}
    for task, raw in raw_tasks.items():
        if task not in supported_tasks:
            raise ValueError(f"unknown task '{task}'")
        name = str(raw or "")
        if name:
            if name not in validated:
                raise ValueError(f"task '{task}' references unknown profile '{name}'")
            bindings[task] = name
    return bindings


def order_profiles(
    enabled: list[str],
    preferred: str | None,
    default_profile: str,
) -> list[str]:
    """Put the preferred (or default) profile first, if enabled."""
    first = preferred or default_profile
    if first in enabled:
        return [first, *[n for n in enabled if n != first]]
    return list(enabled)


def get_available_profiles(
    profiles: dict[str, LLMProfile],
    breaker: ProviderCircuitBreaker,
    *,
    enabled: bool,
    preferred: str | None,
    default_profile: str,
) -> list[str]:
    """Return enabled profile names ordered for rotation, filtered by breaker state."""
    if not enabled:
        return []
    enabled_names = [n for n, p in profiles.items() if p.enabled]
    if not enabled_names:
        return []

    ordered = order_profiles(enabled_names, preferred, default_profile)
    allowed = [n for n in ordered if breaker.allow(n)]
    if allowed:
        return allowed
    half_open = [n for n in ordered if breaker.state(n) == "half_open"]
    return half_open or ordered


async def call_single_profile(  # noqa: PLR0913, PLR0917
    profile: LLMProfile,
    prompt: str,
    context: dict | None,
    build_kwargs: dict | None,
    call_fn: Any,  # noqa: ANN401
    breaker: ProviderCircuitBreaker,
) -> tuple[bool, str]:
    """Call a single profile, recording its health in the circuit breaker."""
    try:
        kwargs = dict(build_kwargs or {})
        if context:
            kwargs.setdefault("context", context)
        result = await call_fn(profile, prompt)
        if result.get("success"):
            breaker.record_success(profile.name)
            return True, result["content"]
        breaker.record_failure(profile.name)
        return False, result.get("error") or "unknown error"
    except Exception as exc:  # noqa: BLE001
        breaker.record_failure(profile.name)
        return False, str(exc)


async def execute_profile_chain(  # noqa: PLR0913, PLR0917
    profiles: dict[str, LLMProfile],
    available_names: list[str],
    prompt: str,
    context: dict | None,
    build_kwargs: dict | None,
    call_profile_fn: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Execute a rotation of profiles via a FallbackChain."""
    if not available_names:
        return {"success": False, "error": "No enabled LLM profiles", "raw": ""}

    def _make_handler(prof: Any) -> FallbackHandler:
        async def _handler() -> tuple[bool, Any]:
            return cast(
                "tuple[bool, Any]", await call_profile_fn(prof, prompt, context, build_kwargs)
            )

        return _handler

    steps: dict[str, FallbackHandler] = {
        name: _make_handler(profiles[name]) for name in available_names
    }
    chain = FallbackChain(steps)
    outcome = await chain.run()
    if outcome.step and outcome.error is None:
        return {"success": True, "content": outcome.value, "profile": outcome.step}
    return {
        "success": False,
        "error": outcome.error or "LLM call failed for all profiles",
        "raw": "",
    }


__all__ = [
    "call_single_profile",
    "execute_profile_chain",
    "get_available_profiles",
    "order_profiles",
    "validate_task_profiles",
]
