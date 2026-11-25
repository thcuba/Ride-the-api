"""OpenAI-compatible client pooling, lifecycle management, and connection testing."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, cast

import httpx
from openai import AsyncOpenAI
from pydantic import SecretStr

from core.llm.models import LLMCallError, LLMProfile
from core.llm.parser import _is_retryable_llm_error
from core.retry import make_retryer

logger = logging.getLogger(__name__)


def _get_async_openai_cls() -> type[AsyncOpenAI]:
    mod = sys.modules.get("core.llm_decipher")
    if mod is not None and hasattr(mod, "AsyncOpenAI"):
        return cast("type[AsyncOpenAI]", mod.AsyncOpenAI)
    return AsyncOpenAI


class LLMClientPool:
    """Manages creation, caching, and lifecycle of AsyncOpenAI clients."""

    def __init__(self) -> None:
        # Keyed by (profile.name, connection_fingerprint)
        self._clients: dict[tuple, AsyncOpenAI] = {}

    @staticmethod
    def client_fingerprint(profile: LLMProfile) -> tuple:
        """Connection-relevant parameters; changing these requires a fresh client."""
        return (
            profile.base_url.rstrip("/"),
            profile.api_key.get_secret_value(),
            profile.timeout,
            profile.max_retries,
        )

    def prune_stale_clients(self, fingerprints: dict[str, tuple]) -> None:
        """Drop cached clients that no longer match configured profiles."""
        for key in list(self._clients):
            name, fingerprint = key
            if fingerprints.get(name) != fingerprint:
                if name in fingerprints:
                    logger.info("LLM profile %r connection changed, recreating client", name)
                self._clients.pop(key)

    def build_client(self, profile: LLMProfile, *, follow_redirects: bool = True) -> AsyncOpenAI:
        """Create a fresh OpenAI-compatible async client for a profile.

        ``follow_redirects`` is turned off by the test endpoint. The base URL is checked
        before the request, and a redirect would carry it to a host that was never
        checked — the classic way around an SSRF guard. The SDK builds its own httpx
        client with redirects on, so the only way to say no is to hand it one.
        """
        import core.llm_decipher as shim_mod  # noqa: PLC0415

        cls = getattr(shim_mod, "AsyncOpenAI", AsyncOpenAI)
        api_key = profile.api_key.get_secret_value() or "not-set"
        extra: dict[str, Any] = {}
        if not follow_redirects:
            extra["http_client"] = httpx.AsyncClient(
                follow_redirects=False,
                timeout=profile.timeout,
            )
        return cls(
            api_key=api_key,
            base_url=profile.base_url.rstrip("/"),
            timeout=profile.timeout,
            max_retries=0,
            **extra,
        )

    def get_client(
        self,
        profile: LLMProfile,
        *,
        builder: Any = None,  # noqa: ANN401
    ) -> AsyncOpenAI:
        """Get or create an OpenAI-compatible async client for a profile."""
        key = (profile.name, self.client_fingerprint(profile))
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        build_fn = builder or self.build_client
        client = build_fn(profile)
        self._clients[key] = client
        return client

    @staticmethod
    async def complete(client: AsyncOpenAI, kwargs: dict[str, Any]) -> str:
        """Get a non-empty completion; raises LLMCallError on empty."""
        completion = await client.chat.completions.create(**kwargs)
        content = (completion.choices[0].message.content or "").strip()
        if not content:
            raise LLMCallError("LLM returned an empty completion")
        return content

    async def call_llm(
        self,
        profile: LLMProfile,
        prompt: str,
        *,
        client_getter: Any = None,  # noqa: ANN401
        complete_fn: Any = None,  # noqa: ANN401
    ) -> dict[str, Any]:
        """Call the LLM API via the OpenAI-compatible SDK with tenacity retries."""
        get_client = client_getter or self.get_client
        complete = complete_fn or self.complete

        client = get_client(profile)
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
        if profile.max_tokens > 0:
            kwargs["max_tokens"] = profile.max_tokens

        retryer = make_retryer(
            max_attempts=profile.max_retries + 1, retry_if=_is_retryable_llm_error
        )
        error: str | None = None
        try:
            async for attempt in retryer:
                with attempt:
                    try:
                        content = await complete(client, kwargs)
                    except Exception as e:  # noqa: BLE001
                        error = f"LLM call failed: {e}"
                        logger.warning("LLM call error (retrying): %s", e)
                        raise
                    else:
                        return {"success": True, "content": content}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": error or f"LLM call failed: {e}", "raw": ""}
        return {"success": False, "error": error or "LLM call failed", "raw": ""}

    async def test_connection(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key: str = "",
        timeout: int = 30,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Send a test 'hello' completion to verify connectivity and authentication."""
        profile = LLMProfile(
            name="_test",
            base_url=base_url,
            api_key=SecretStr(api_key),
            model_id=model_id,
            prompt_template="",
            enabled=True,
            timeout=timeout,
            max_retries=max_retries,
        )
        client = self.build_client(profile, follow_redirects=False)
        kwargs = {
            "model": profile.model_id,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
        }
        try:
            content = await asyncio.wait_for(self.complete(client, kwargs), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM connection test failed for %s: %s", base_url, exc)
            return {"success": False, "error": f"LLM test failed: {exc}"}
        finally:
            await client.close()
        return {"success": True, "content": content, "message": "Test passed: got a response"}

    async def close(self) -> None:
        """Close all open OpenAI SDK clients."""
        for client in self._clients.values():
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("Error closing LLM client: %s", e)
        self._clients.clear()


__all__ = ["LLMClientPool"]
