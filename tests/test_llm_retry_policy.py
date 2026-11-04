"""One retry layer for LLM calls, honouring config (R-9)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai import AuthenticationError, InternalServerError

from core.config import Config
from core.llm_decipher import LLMDecipherService
from tests.test_llm_decipher import MockConfigManager


def _service(**profile_overrides) -> LLMDecipherService:
    profile = {"base_url": "http://llm.invalid/v1", "api_key": "k", "model_id": "m"}
    profile.update(profile_overrides)
    cm = MockConfigManager()
    cm.config = Config(**{"llm_decipher": {"profiles": {"default": profile}}})
    with patch("core.llm_decipher.load_llm_settings", return_value=None):
        return LLMDecipherService(config_manager=cm)


def _status_error(cls, status: int):
    request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


def test_yaml_timeout_and_retries_are_kept():
    service = _service(timeout=120, max_retries=0)
    profile = service.get_profile("default")
    assert profile.timeout == 120  # noqa: PLR2004
    assert profile.max_retries == 0


@patch("core.llm_decipher.AsyncOpenAI")
def test_sdk_retries_are_disabled(mock_client_class):
    service = _service(max_retries=2)
    service._get_client(service.get_profile("default"))
    assert mock_client_class.call_args.kwargs["max_retries"] == 0


@pytest.mark.asyncio
async def test_auth_error_is_not_retried():
    service = _service(max_retries=2)
    complete = AsyncMock(side_effect=_status_error(AuthenticationError, 401))
    with patch.object(service, "_complete", complete):
        result = await service.call_llm(service.get_profile("default"), "prompt")
    assert result["success"] is False
    assert complete.await_count == 1


@pytest.mark.asyncio
async def test_server_error_is_retried_up_to_max_retries():
    service = _service(max_retries=1)
    complete = AsyncMock(side_effect=_status_error(InternalServerError, 500))
    with patch.object(service, "_complete", complete):
        result = await service.call_llm(service.get_profile("default"), "prompt")
    assert result["success"] is False
    assert complete.await_count == 2  # first attempt + one retry  # noqa: PLR2004
