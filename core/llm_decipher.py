"""
LLM Deciphering Service - Uses configurable LLMs to analyze and decipher
intercepted request/response pairs for protocol understanding and field mapping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import json_repair
from openai import AsyncOpenAI
from pydantic import BaseModel, SecretStr, field_validator

from core.config import get_config_manager
from core.fallback_policy import FallbackChain, ProviderCircuitBreaker
from core.retry import make_retryer

logger = logging.getLogger(__name__)


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
        except Exception:  # noqa: BLE001 - best-effort repair
            return None


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

    @field_validator("api_key", mode="before")
    @classmethod
    def _resolve_env_var(cls, v: str) -> str:
        """Resolve API key from environment variable if it starts with ${}."""
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            return os.environ.get(v[2:-1], "")
        return v if isinstance(v, str) else ""


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


class LLMDecipherService:
    """
    Service that uses LLMs to decipher intercepted device communications.
    Supports multiple LLM providers (OpenAI-compatible APIs, local Ollama, etc.)
    """

    def __init__(self, config_manager=None) -> None:
        self.config_manager = config_manager or get_config_manager()
        self._config = None
        self._profiles: dict[str, LLMProfile] = {}
        self._default_profile = "default"
        self._clients: dict[str, AsyncOpenAI] = {}
        self._cache: dict[str, DecipherResult] = {}  # Simple in-memory cache
        self._cache_ttl = 3600  # 1 hour
        # Circuit breaker between provider profiles: a repeatedly failing
        # profile is temporarily excluded from rotation and probed again
        # after a cooldown, so a degraded provider doesn't kill analysis
        # when a healthy alternative profile exists.
        self.profile_breaker = ProviderCircuitBreaker()

        self._load_config()

    def _load_config(self):
        """Load LLM deciphering configuration."""
        config = self.config_manager.config
        self._config = getattr(config, "llm_decipher", None)

        if not self._config:
            logger.warning("No LLM decipher config found, using defaults")
            return

        self._default_profile = getattr(self._config, "default_profile", "default")

        # Load profiles
        profiles_config = getattr(self._config, "profiles", {})
        for name, profile_config in profiles_config.items():
            profile = LLMProfile(
                name=name,
                base_url=getattr(profile_config, "base_url", "https://api.openai.com/v1"),
                api_key=getattr(profile_config, "api_key", ""),
                model_id=getattr(profile_config, "model_id", "gpt-4o-mini"),
                prompt_template=getattr(profile_config, "prompt_template", ""),
                enabled=getattr(profile_config, "enabled", True),
                timeout=getattr(profile_config, "timeout", 30),
                max_retries=getattr(profile_config, "max_retries", 2),
            )
            self._profiles[name] = profile

        # Register for config changes
        self.config_manager.register_callback(self._on_config_change)

        logger.info(f"Loaded {len(self._profiles)} LLM profiles: {list(self._profiles.keys())}")

    def _on_config_change(self, _new_config):
        """Reload config on change."""
        logger.info("LLM decipher config changed, reloading")
        self._load_config()

    def _get_client(self, profile: LLMProfile) -> AsyncOpenAI:
        """Get or create an OpenAI-compatible async client for a profile."""
        cached = self._clients.get(profile.name)
        if cached:
            return cached
        # The SDK rejects an empty/missing api_key at construction, so pass a
        # placeholder for local providers (e.g. Ollama) that ignore auth.
        api_key = profile.api_key.get_secret_value() or "not-set"
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=profile.base_url.rstrip("/"),
            timeout=profile.timeout,
            max_retries=profile.max_retries,
        )
        self._clients[profile.name] = client
        return client

    def get_profile(self, name: str | None = None) -> LLMProfile | None:
        """Get LLM profile by name, or default."""
        profile_name = name or self._default_profile
        return self._profiles.get(profile_name)

    def list_profiles(self) -> list[str]:
        """List available profile names."""
        return [name for name, p in self._profiles.items() if p.enabled]

    def available_profiles(self, preferred: str | None = None) -> list[str]:
        """Return enabled profile names ordered for rotation.

        The ``preferred`` profile (or the default) comes first when it is not
        currently open in the circuit breaker. The remaining enabled profiles
        follow in declaration order, so a degraded provider never blocks the
        whole path.
        """
        enabled = [n for n, p in self._profiles.items() if p.enabled]
        if not enabled:
            return []

        ordered = self._order_profiles(enabled, preferred)
        # Filter out circuit-open providers unless none are usable.
        allowed = [n for n in ordered if self.profile_breaker.allow(n)]
        if allowed:
            return allowed
        # Everything is open: allow a half-open probe of the first profile.
        half_open = [n for n in ordered if self.profile_breaker.state(n) == "half_open"]
        return half_open or ordered

    def _order_profiles(self, enabled: list[str], preferred: str | None) -> list[str]:
        """Put the preferred (or default) profile first, if enabled."""
        first = preferred or self._default_profile
        if first in enabled:
            return [first, *[n for n in enabled if n != first]]
        return list(enabled)

    async def call_profile_chain(  # noqa: PLR0913
        self,
        *,
        prompt: str,
        preferred: str | None = None,
        context: dict[str, Any] | None = None,
        build_kwargs: dict[str, Any] | None = None,
    ) -> dict:
        """Call profiles in rotation via a :class:`FallbackChain`.

        On success the winning profile is recorded as healthy; failures mark
        the profile in the circuit breaker before the next profile is tried.
        Returns the same shape as :meth:`call_llm`.
        """
        profiles = self.available_profiles(preferred)
        if not profiles:
            return {"success": False, "error": "No enabled LLM profiles", "raw": ""}

        handlers = OrderedDict(
            (
                name,
                (
                    lambda p=self._profiles[name]: self._call_profile(
                        p, prompt, context, build_kwargs
                    )
                ),
            )
            for name in profiles
        )
        chain = FallbackChain(handlers)
        outcome = await chain.run()
        if outcome.step and outcome.error is None:
            return {"success": True, "content": outcome.value, "profile": outcome.step}
        return {
            "success": False,
            "error": outcome.error or "LLM call failed for all profiles",
            "raw": "",
        }

    async def _call_profile(
        self,
        profile: LLMProfile,
        prompt: str,
        context: dict | None,
        build_kwargs: dict | None,
    ) -> tuple[bool, str]:
        """Call a single profile, recording its health in the breaker."""
        try:
            kwargs = dict(build_kwargs or {})
            if context:
                kwargs.setdefault("context", context)
            result = await self.call_llm(profile, prompt)
            if result.get("success"):
                self.profile_breaker.record_success(profile.name)
                return True, result["content"]
            self.profile_breaker.record_failure(profile.name)
            return False, result.get("error") or "unknown error"
        except Exception as exc:  # noqa: BLE001
            self.profile_breaker.record_failure(profile.name)
            return False, str(exc)

    async def decipher_with_params(
        self,
        context: dict,
        base_url: str | None = None,
        model_id: str | None = None,
        profile_name: str | None = None,
    ) -> dict:
        """Decipher using on-the-fly overridden parameters. Used by pipeline."""
        profile = self.get_profile(profile_name)
        if not profile:
            return {"success": False, "error": f"Profile not found: {profile_name}"}

        # Override base_url/model_id if provided
        effective = profile
        if base_url or model_id:
            effective = LLMProfile(
                name=profile.name,
                base_url=base_url or profile.base_url,
                api_key=profile.api_key,
                model_id=model_id or profile.model_id,
                prompt_template=profile.prompt_template,
                enabled=profile.enabled,
                timeout=profile.timeout,
                max_retries=profile.max_retries,
            )

        pairs_text = json.dumps(context.get("pairs", []), indent=2, default=str)
        prompt = effective.prompt_template
        replacements = {
            "{vendor}": context.get("vendor", "unknown"),
            "{device_type}": context.get("device_type", "unknown"),
            "{pairs}": pairs_text,
            "{device_id}": context.get("device_id", "unknown"),
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)

        try:
            result = await self.call_llm(effective, prompt)
            if result["success"]:
                content = result["content"]
                analysis = _parse_llm_json(content)
                if analysis is not None:
                    return {"success": True, "analysis": analysis}
                return {"success": False, "error": "Unable to parse LLM analysis JSON"}
            return {"success": False, "error": result.get("error")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _build_prompt(
        self,
        profile: LLMProfile,
        pair: RequestResponsePair,
        db_schema: str,
        recent_patterns: list[dict],
    ) -> str:
        """Build the prompt for the LLM."""
        # Format request/response pairs
        req = pair.request
        resp = pair.response

        pairs_text = f"""
Request:
  Method: {req.method}
  Path: {req.path}
  Headers: {json.dumps(req.headers, indent=2)}
  Query Params: {json.dumps(req.query_params, indent=2)}
  Body: {json.dumps(req.body, indent=2) if req.body else "empty"}

Response:
  Status: {resp.status_code}
  Headers: {json.dumps(resp.headers, indent=2)}
  Body: {json.dumps(resp.body, indent=2) if resp.body else "empty"}
  Latency: {resp.latency_ms:.1f}ms
"""

        # Use safe replacement to avoid issues with literal braces in JSON content
        prompt = profile.prompt_template
        replacements = {
            "{vendor}": pair.vendor,
            "{device_type}": pair.request.metadata.get("device_type", "unknown"),
            "{db_schema}": db_schema,
            "{recent_patterns}": json.dumps(recent_patterns, indent=2),
            "{pairs}": pairs_text,
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    async def decipher_pair(
        self,
        pair: RequestResponsePair,
        profile_name: str | None = None,
    ) -> DecipherResult:
        """
        Send a request/response pair to LLM for deciphering.
        Returns structured analysis of the protocol.
        """
        start_time = time.time()

        profile = self.get_profile(profile_name)
        if not profile:
            return DecipherResult(
                pair_id=pair.pair_id,
                device_id=pair.device_id,
                vendor=pair.vendor,
                intent="unknown",
                fields={},
                confidence=0.0,
                success=False,
                error=f"Profile not found: {profile_name or self._default_profile}",
                processing_time_ms=(time.time() - start_time) * 1000,
            )

        # Check cache
        cache_key = f"{pair.pair_id}:{profile.name}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if (datetime.now(UTC) - cached.timestamp).total_seconds() < self._cache_ttl:
                logger.debug(f"Cache hit for pair {pair.pair_id}")
                return cached

        # Get database schema (simplified)
        db_schema = self._get_db_schema(pair.vendor)

        # Get recent patterns for this vendor/device_type
        recent_patterns = self._get_recent_patterns(
            pair.vendor, pair.request.metadata.get("device_type")
        )

        # Build prompt
        prompt = self._build_prompt(profile, pair, db_schema, recent_patterns)

        # Call LLM
        result = await self.call_llm(profile, prompt)

        processing_time = (time.time() - start_time) * 1000

        if not result["success"]:
            return DecipherResult(
                pair_id=pair.pair_id,
                device_id=pair.device_id,
                vendor=pair.vendor,
                intent="unknown",
                fields={},
                confidence=0.0,
                success=False,
                error=result["error"],
                processing_time_ms=processing_time,
                raw_response=result.get("raw", ""),
            )

        # Parse LLM response
        try:
            analysis = _parse_llm_json(result["content"])
        except Exception:  # noqa: BLE001 - defensive, mirrors old JSONDecodeError path
            analysis = None
        if analysis is None:
            err = f"Failed to parse LLM response: {result['content'][:80]!r}"
            logger.error(err)  # noqa: TRY400
            return DecipherResult(
                pair_id=pair.pair_id,
                device_id=pair.device_id,
                vendor=pair.vendor,
                intent="unknown",
                fields={},
                confidence=0.0,
                success=False,
                error=err,
                processing_time_ms=processing_time,
                raw_response=result["content"],
            )

        # Create result
        decipher_result = DecipherResult(
            pair_id=pair.pair_id,
            device_id=pair.device_id,
            vendor=pair.vendor,
            intent=analysis.get("intent", "unknown"),
            fields=analysis.get("fields", {}),
            confidence=float(analysis.get("confidence", 0.0)),
            suggested_dp_codes=analysis.get("suggested_dp_codes", {}),
            protocol_notes=analysis.get("protocol_notes", ""),
            raw_response=result["content"],
            success=True,
            processing_time_ms=processing_time,
        )

        # Cache result
        self._cache[cache_key] = decipher_result

        logger.info(
            f"Deciphered pair {pair.pair_id}: "
            f"intent={decipher_result.intent}, confidence={decipher_result.confidence:.2f}"
        )
        return decipher_result

    async def call_llm(self, profile: LLMProfile, prompt: str) -> dict:
        """Call the LLM API via the OpenAI-compatible SDK with tenacity retries."""
        client = self._get_client(profile)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a protocol analysis expert. Analyze IoT device "
                    "communications and output structured JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": profile.model_id,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        retryer = make_retryer(max_attempts=profile.max_retries + 1)
        error: str | None = None
        try:
            async for attempt in retryer:
                with attempt:
                    try:
                        return {"success": True, "content": await self._complete(client, kwargs)}
                    except Exception as e:  # noqa: BLE001
                        error = f"LLM call failed: {e}"
                        logger.warning("LLM call error (retrying): %s", e)
                        raise
        except Exception as e:  # noqa: BLE001
            # All retries exhausted; tenacity re-raises RetryError here.
            return {"success": False, "error": error or f"LLM call failed: {e}", "raw": ""}
        return {"success": False, "error": error or "LLM call failed", "raw": ""}

    async def _complete(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> str:
        """Get a non-empty completion; raises :class:`LLMCallError` on empty."""
        completion = await client.chat.completions.create(**kwargs)
        content = (completion.choices[0].message.content or "").strip()
        if not content:
            raise LLMCallError("LLM returned an empty completion")
        return content

    def _get_db_schema(self, vendor: str) -> str:
        """Get database schema for vendor (simplified)."""
        # In production, this would query the actual schema
        return f"""
Vendor: {vendor}
Tables:
- devices (device_id, name, type, capabilities, config)
- readings (device_id, timestamp, temp_target, temp_actual, humidity, power_watts, mode, fan_speed)
- commands (device_id, timestamp, command_type, params, success, response)
- models (vendor, device_type, model_path, accuracy, version)
- policies (vendor, device_type, rules_json)
- intercepted_requests (device_id, protocol, method, path, body, parsed_intent, parsed_params)
"""

    def _get_recent_patterns(self, _vendor: str, _device_type: str | None) -> list[dict]:
        """Get recent successful deciphering patterns for context."""
        return []
        # TODO: Load from device database patterns table  # noqa: ERA001
        # patterns = await db.get_recent_patterns(vendor, limit=10)  # noqa: ERA001

    async def decipher_batch(
        self,
        pairs: list[RequestResponsePair],
        profile_name: str | None = None,
    ) -> list[DecipherResult]:
        """Decipher multiple pairs in parallel."""
        tasks = [self.decipher_pair(pair, profile_name) for pair in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        deciphered = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Batch decipher error for pair {pairs[i].pair_id}: {result}")
                deciphered.append(
                    DecipherResult(
                        pair_id=pairs[i].pair_id,
                        device_id=pairs[i].device_id,
                        vendor=pairs[i].vendor,
                        intent="error",
                        fields={},
                        confidence=0.0,
                        success=False,
                        error=str(result),
                    )
                )
            else:
                deciphered.append(result)

        return deciphered

    async def close(self):
            """Close all open OpenAI SDK clients."""
            for client in self._clients.values():
                try:
                    await client.close()
                except Exception as e:  # noqa: BLE001
                    logger.debug("Error closing LLM client: %s", e)
            self._clients.clear()


# Global instance
_llm_decipher: LLMDecipherService | None = None


def get_llm_decipher() -> LLMDecipherService:
    """Get or create global LLM decipher service instance."""
    global _llm_decipher  # noqa: PLW0603
    if _llm_decipher is None:
        _llm_decipher = LLMDecipherService()
    return _llm_decipher
