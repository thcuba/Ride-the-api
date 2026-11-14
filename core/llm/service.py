"""Core LLM deciphering service orchestrating profiles, clients, prompts, and inference."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache
from pydantic import ValidationError

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from core.config import SECRET_MASK, get_config_manager, is_env_reference
from core.fallback_policy import ProviderCircuitBreaker
from core.llm.client_pool import LLMClientPool
from core.llm.models import (
    DecipherResult,
    LLMDecipherAnalysis,
    LLMProfile,
    RequestResponsePair,
)
from core.llm.parser import _parse_llm_json, _raw_secret, _strict_true
from core.llm.profiles import (
    call_single_profile,
    execute_profile_chain,
    get_available_profiles,
    order_profiles,
    validate_task_profiles,
)
from core.llm.prompts import (
    build_decipher_prompt,
    build_params_prompt,
    build_validation_prompt,
    get_default_db_schema,
    get_recent_patterns,
)
from core.llm_settings import load_llm_settings, save_llm_settings
from core.pattern_utils import safe_float as _safe_float

logger = logging.getLogger(__name__)

_CACHE_MAX_SIZE = 512
_CACHE_TTL = 3600  # 1 hour


def _get_load_llm_settings():
    import core.llm_decipher as shim_mod  # noqa: PLC0415

    return getattr(shim_mod, "load_llm_settings", load_llm_settings)


def _get_save_llm_settings():
    import core.llm_decipher as shim_mod  # noqa: PLC0415

    return getattr(shim_mod, "save_llm_settings", save_llm_settings)


class LLMDecipherService:
    """Service that uses LLMs to decipher intercepted device communications."""

    TASKS: dict[str, str] = {
        "correlation": "Validate an uncertain match against the learned patterns",
        "flush": "Analyse the buffer and learn patterns (Send to LLM)",
        "learning_context": "Analyse the buffer grounded with the already-learned model",
    }

    def __init__(self, config_manager=None) -> None:
        self.config_manager = config_manager or get_config_manager()
        self._config = None
        self._profiles: dict[str, LLMProfile] = {}
        self._task_profiles: dict[str, str] = {}
        self._default_profile = "default"
        self._enabled = True

        # Delegated client pool
        self.client_pool = LLMClientPool()
        self._client_fingerprints: dict[str, tuple] = {}
        self._raw_api_keys: dict[str, str] = {}

        self._cache_ttl = _CACHE_TTL
        self._cache: TTLCache = TTLCache(maxsize=_CACHE_MAX_SIZE, ttl=self._cache_ttl)
        self.profile_breaker = ProviderCircuitBreaker()
        self._pattern_loader = None

        self._load_config()
        self.config_manager.register_callback(self._on_config_change)

    # ── Backward-compatible properties & delegators for client_pool ──────────

    @property
    def _clients(self) -> dict[tuple, AsyncOpenAI]:
        return self.client_pool._clients

    @_clients.setter
    def _clients(self, val: dict[tuple, AsyncOpenAI]) -> None:
        self.client_pool._clients = val

    def _client_fingerprint(self, profile: LLMProfile) -> tuple:
        return self.client_pool.client_fingerprint(profile)

    def _prune_stale_clients(self, fingerprints: dict[str, tuple]) -> None:
        self.client_pool.prune_stale_clients(fingerprints)

    def _build_client(self, profile: LLMProfile) -> AsyncOpenAI:
        return self.client_pool.build_client(profile)

    def _get_client(self, profile: LLMProfile) -> AsyncOpenAI:
        return self.client_pool.get_client(profile)

    async def _complete(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> str:
        return await self.client_pool.complete(client, kwargs)

    # ── Configuration loading & management ───────────────────────────────────

    def _load_config(self) -> None:  # noqa: C901
        """Load LLM deciphering configuration."""
        config = self.config_manager.config
        self._config = getattr(config, "llm_decipher", None)

        if not self._config:
            logger.warning("No LLM decipher config found, using defaults")
            return

        self._default_profile = getattr(self._config, "default_profile", "default")
        self._enabled = bool(getattr(self._config, "enabled", True))
        self._task_profiles = {
            task: str(name)
            for task, name in (getattr(self._config, "task_profiles", None) or {}).items()
            if task in self.TASKS and name
        }

        profiles_config = getattr(self._config, "profiles", {})
        new_profiles: dict[str, LLMProfile] = {}
        new_raw_keys: dict[str, str] = {}
        for name, profile_config in profiles_config.items():
            new_raw_keys[name] = _raw_secret(getattr(profile_config, "api_key", ""))
            profile = LLMProfile(
                name=name,
                base_url=getattr(profile_config, "base_url", "https://api.openai.com/v1"),
                api_key=new_raw_keys[name],
                model_id=getattr(profile_config, "model_id", "gpt-4o-mini"),
                prompt_template=getattr(profile_config, "prompt_template", ""),
                enabled=getattr(profile_config, "enabled", True),
                timeout=getattr(profile_config, "timeout", 30),
                max_retries=getattr(profile_config, "max_retries", 2),
                max_tokens=getattr(profile_config, "max_tokens", 2048),
            )
            new_profiles[name] = profile

        overrides = _get_load_llm_settings()()
        if overrides:
            if "enabled" in overrides:
                self._enabled = bool(overrides["enabled"])
            if "default_profile" in overrides:
                self._default_profile = overrides["default_profile"]
            if isinstance(overrides.get("task_profiles"), dict):
                self._task_profiles = {
                    task: str(name)
                    for task, name in overrides["task_profiles"].items()
                    if task in self.TASKS and name
                }
            if "profiles" in overrides:
                runtime_profiles = overrides.get("profiles")
                if isinstance(runtime_profiles, dict):
                    new_profiles = {}
                    new_raw_keys = {}
                    for name, pcfg in runtime_profiles.items():
                        if not isinstance(pcfg, dict):
                            continue
                        new_raw_keys[name] = _raw_secret(pcfg.get("api_key", ""))
                        new_profiles[name] = LLMProfile(
                            name=name,
                            base_url=pcfg.get("base_url", "https://api.openai.com/v1"),
                            api_key=new_raw_keys[name],
                            model_id=pcfg.get("model_id", "gpt-4o-mini"),
                            prompt_template=pcfg.get("prompt_template", ""),
                            enabled=pcfg.get("enabled", True),
                            timeout=pcfg.get("timeout", 30),
                            max_retries=pcfg.get("max_retries", 2),
                            max_tokens=int(pcfg.get("max_tokens", 2048)),
                        )

        new_fingerprints = {name: self._client_fingerprint(p) for name, p in new_profiles.items()}
        self._prune_stale_clients(new_fingerprints)
        self._client_fingerprints = new_fingerprints

        self._profiles = new_profiles
        self._raw_api_keys = new_raw_keys

        logger.info(f"Loaded {len(self._profiles)} LLM profiles: {list(self._profiles.keys())}")

    def _on_config_change(self, _new_config) -> None:
        """Reload config on change."""
        logger.info("LLM decipher config changed, reloading")
        self._load_config()

    def reload(self) -> None:
        """Re-read config.yaml and runtime overrides."""
        self._load_config()

    # ── Profile querying & settings ──────────────────────────────────────────

    def get_profile(self, name: str | None = None) -> LLMProfile | None:
        """Get LLM profile by name, or default."""
        profile_name = name or self._default_profile
        return self._profiles.get(profile_name)

    def resolve_task_profile(self, task: str) -> str | None:
        """Return the profile bound to task, or None when unbound."""
        name = self._task_profiles.get(task)
        return name if name and name in self._profiles else None

    def list_profiles(self) -> list[str]:
        """List available enabled profile names."""
        if not self._enabled:
            return []
        return [name for name, p in self._profiles.items() if p.enabled]

    def is_enabled(self) -> bool:
        """Whether LLM deciphering is globally enabled."""
        return self._enabled

    def get_settings(self, *, include_secrets: bool = False) -> dict:
        """Return the effective LLM settings for dashboard or internal callers."""
        profiles = {}
        for name, p in self._profiles.items():
            profiles[name] = {
                "base_url": p.base_url,
                "api_key": (
                    p.api_key.get_secret_value() if include_secrets else self._public_api_key(name)
                ),
                "model_id": p.model_id,
                "prompt_template": p.prompt_template,
                "enabled": p.enabled,
                "timeout": p.timeout,
                "max_retries": p.max_retries,
                "max_tokens": p.max_tokens,
            }
        return {
            "enabled": self._enabled,
            "default_profile": self._default_profile,
            "profiles": profiles,
            "task_profiles": {task: self._task_profiles.get(task, "") for task in self.TASKS},
        }

    def update_settings(self, settings: dict) -> dict:
        """Apply dashboard-provided LLM settings and persist them."""
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")  # noqa: TRY004

        enabled = bool(settings.get("enabled", self._enabled))
        default_profile = settings.get("default_profile", self._default_profile)
        profiles = settings.get("profiles", {})

        if not isinstance(profiles, dict):
            raise ValueError("profiles must be an object")  # noqa: TRY004

        validated: dict[str, dict] = {}
        for name, pcfg in profiles.items():
            if not isinstance(pcfg, dict):
                raise ValueError(f"profile '{name}' must be an object")  # noqa: TRY004
            api_key = str(pcfg.get("api_key", ""))
            if api_key == SECRET_MASK:
                api_key = self._raw_api_keys.get(name, "")
            validated[name] = {
                "base_url": str(pcfg.get("base_url", "https://api.openai.com/v1")),
                "api_key": api_key,
                "model_id": str(pcfg.get("model_id", "gpt-4o-mini")),
                "prompt_template": str(pcfg.get("prompt_template", "")),
                "enabled": bool(pcfg.get("enabled", True)),
                "timeout": int(pcfg.get("timeout", 30)),
                "max_retries": int(pcfg.get("max_retries", 2)),
                "max_tokens": int(pcfg.get("max_tokens", 2048)),
            }

        if default_profile and default_profile not in validated:
            raise ValueError(f"default_profile '{default_profile}' is not a known profile")

        task_profiles = self._validate_task_profiles(settings.get("task_profiles"), validated)

        payload = {
            "enabled": enabled,
            "default_profile": default_profile,
            "profiles": validated,
            "task_profiles": task_profiles,
        }
        _get_save_llm_settings()(payload)
        self._load_config()
        return self.get_settings()

    def _public_api_key(self, name: str) -> str:
        """A profile's API key as the dashboard may see it."""
        raw = self._raw_api_keys.get(name)
        if raw is None:
            profile = self._profiles.get(name)
            raw = profile.api_key.get_secret_value() if profile else ""
        if is_env_reference(raw):
            return raw
        return SECRET_MASK if raw else ""

    def get_settings_for_persistence(self) -> dict:
        """Settings in the form they may be written to disk."""
        settings = self.get_settings(include_secrets=True)
        for name, pcfg in settings["profiles"].items():
            pcfg["api_key"] = self._raw_api_keys.get(name, pcfg["api_key"])
        return settings

    def stored_api_key(self, name: str) -> str:
        """The resolved key of profile name (empty string when unknown)."""
        profile = self._profiles.get(name)
        return profile.api_key.get_secret_value() if profile else ""

    @staticmethod
    def _validate_task_profiles(raw_tasks: Any, validated: dict[str, dict]) -> dict[str, str]:  # noqa: ANN401
        return validate_task_profiles(raw_tasks, validated, LLMDecipherService.TASKS)

    def _order_profiles(self, enabled: list[str], preferred: str | None) -> list[str]:
        return order_profiles(enabled, preferred, self._default_profile)

    def available_profiles(self, preferred: str | None = None) -> list[str]:
        return get_available_profiles(
            self._profiles,
            self.profile_breaker,
            enabled=self._enabled,
            preferred=preferred,
            default_profile=self._default_profile,
        )

    # ── Inference & Fallback Execution ───────────────────────────────────────

    async def call_llm(self, profile: LLMProfile, prompt: str) -> dict:
        """Call LLM API via client pool with retries."""
        return await self.client_pool.call_llm(
            profile,
            prompt,
            client_getter=self._get_client,
            complete_fn=self._complete,
        )

    async def call_profile_chain(  # noqa: PLR0913
        self,
        *,
        prompt: str,
        preferred: str | None = None,
        context: dict[str, Any] | None = None,
        build_kwargs: dict[str, Any] | None = None,
    ) -> dict:
        """Call profiles in rotation via a FallbackChain."""
        profiles = self.available_profiles(preferred)
        return await execute_profile_chain(
            self._profiles,
            profiles,
            prompt,
            context,
            build_kwargs,
            self.call_llm,
            self.profile_breaker,
        )

    async def _call_profile(
        self,
        profile: LLMProfile,
        prompt: str,
        context: dict | None = None,
        build_kwargs: dict | None = None,
    ) -> tuple[bool, str]:
        """Call a single profile, recording its health in the breaker."""
        return await call_single_profile(
            profile,
            prompt,
            context,
            build_kwargs,
            self.call_llm,
            self.profile_breaker,
        )

    async def test_connection(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key: str = "",
        timeout: int = 30,
        max_retries: int = 2,
    ) -> dict:
        """Send a test completion to verify provider availability."""
        return await self.client_pool.test_connection(
            base_url=base_url,
            model_id=model_id,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    # ── Deciphering Workflows ────────────────────────────────────────────────

    _build_validation_prompt = staticmethod(build_validation_prompt)

    def _build_prompt(
        self,
        profile: LLMProfile,
        pair: RequestResponsePair,
        db_schema: str,
        recent_patterns: list[dict],
    ) -> str:
        return build_decipher_prompt(profile, pair, db_schema, recent_patterns)

    def _get_db_schema(self, vendor: str) -> str:
        return get_default_db_schema(vendor)

    def _get_recent_patterns(self, vendor: str, device_type: str | None) -> list[dict]:
        return get_recent_patterns(self._pattern_loader, vendor, device_type)

    def set_pattern_loader(self, loader) -> None:
        """Register a callable(vendor, device_type) -> list[dict] provider."""
        self._pattern_loader = loader

    async def validate_match(
        self,
        pattern: Any,  # noqa: ANN401
        request: dict,
        profile_name: str | None = None,
    ) -> dict:
        """Ask the LLM whether a below-threshold pattern really applies."""
        profile = self.get_profile(profile_name)
        if not profile:
            return {"applicable": False, "reason": f"profile not found: {profile_name}"}
        prompt = self._build_validation_prompt(pattern, request)
        try:
            result = await self.call_llm(profile, prompt)
        except Exception:  # noqa: BLE001
            logger.exception("LLM match validation failed")
            return {"applicable": False, "reason": "llm error"}
        if not result.get("success"):
            return {"applicable": False, "reason": str(result.get("error") or "llm error")}
        try:
            data = _parse_llm_json(result.get("content") or "")
        except Exception:  # noqa: BLE001
            logger.warning("Could not parse LLM match validation output")
            return {"applicable": False, "reason": "unparseable output"}
        if not isinstance(data, dict):
            return {"applicable": False, "reason": "unexpected output"}
        return {
            "applicable": _strict_true(data.get("applicable", False)),
            "confidence": _safe_float(data.get("confidence"), 0.0),
            "reason": str(data.get("reason") or ""),
        }

    async def decipher_with_params(
        self,
        context: dict,
        base_url: str | None = None,
        model_id: str | None = None,
        profile_name: str | None = None,
    ) -> dict:
        """Decipher using on-the-fly overridden parameters."""
        profile = self.get_profile(profile_name)
        if not profile:
            return {"success": False, "error": f"Profile not found: {profile_name}"}

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
                max_tokens=profile.max_tokens,
            )

        prompt = build_params_prompt(effective.prompt_template, context)

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

    async def decipher_pair(
        self,
        pair: RequestResponsePair,
        profile_name: str | None = None,
    ) -> DecipherResult:
        """Send a request/response pair to LLM for deciphering."""
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

        cache_key = f"{pair.pair_id}:{profile.name}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.debug(f"Cache hit for pair {pair.pair_id}")
            return cached

        db_schema = self._get_db_schema(pair.vendor)
        request_meta = getattr(pair.request, "metadata", {}) or {}
        recent_patterns = self._get_recent_patterns(pair.vendor, request_meta.get("device_type"))

        prompt = self._build_prompt(profile, pair, db_schema, recent_patterns)
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

        try:
            analysis = _parse_llm_json(result["content"])
        except Exception:  # noqa: BLE001
            analysis = None
        if not isinstance(analysis, dict):
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

        try:
            parsed = LLMDecipherAnalysis.model_validate(analysis)
        except ValidationError as e:
            logger.debug("LLM analysis failed validation, using defaults: %s", e)
            parsed = LLMDecipherAnalysis()

        decipher_result = DecipherResult(
            pair_id=pair.pair_id,
            device_id=pair.device_id,
            vendor=pair.vendor,
            intent=parsed.intent,
            fields=parsed.fields,
            confidence=_safe_float(parsed.confidence),
            suggested_dp_codes=parsed.suggested_dp_codes,
            protocol_notes=parsed.protocol_notes,
            raw_response=result["content"],
            success=True,
            processing_time_ms=processing_time,
        )
        self._cache[cache_key] = decipher_result

        logger.info(
            f"Deciphered pair {pair.pair_id}: "
            f"intent={decipher_result.intent}, confidence={decipher_result.confidence:.2f}"
        )
        return decipher_result

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

    async def close(self) -> None:
        """Close all open client pool connections."""
        await self.client_pool.close()


# Global instance
_llm_decipher: LLMDecipherService | None = None


def get_llm_decipher() -> LLMDecipherService:
    """Get or create global LLM decipher service instance."""
    global _llm_decipher  # noqa: PLW0603
    if _llm_decipher is None:
        _llm_decipher = LLMDecipherService()
    return _llm_decipher


__all__ = [
    "LLMDecipherService",
    "_llm_decipher",
    "get_llm_decipher",
]
