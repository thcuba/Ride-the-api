"""
Tests for the LLM Deciphering Service.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm_decipher import (
    DecipherResult,
    LLMDecipherService,
    LLMProfile,
    _parse_llm_json,
)


class MockConfig:
    """Mock for config objects with attribute access."""

    def __init__(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockConfigManager:
    """Mock config manager that doesn't load from YAML."""

    def __init__(self) -> None:
        self.config = MockConfig()
        self.callbacks = []

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def add_change_callback(self, callback):
        self.callbacks.append(callback)


def make_service():
    """Create LLMDecipherService with mocked config manager."""
    cm = MockConfigManager()
    return LLMDecipherService(config_manager=cm)


class TestLLMProfile:
    def test_profile_creation(self):
        profile = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="sk-test123",
            model_id="gpt-4o-mini",
            prompt_template="Analyze this: {pairs}",
        )
        assert profile.name == "test"
        assert profile.base_url == "https://api.openai.com/v1"
        assert profile.model_id == "gpt-4o-mini"
        assert profile.enabled is True
        assert profile.timeout == 30  # noqa: PLR2004
        assert profile.max_retries == 2  # noqa: PLR2004


class TestLLMDecipherService:
    def test_initialization_no_config(self):
        """Service initializes with mock config."""
        service = make_service()
        assert service is not None
        assert service._profiles == {}
        assert service._default_profile == "default"

    def test_list_profiles_empty(self):
        service = make_service()
        assert service.list_profiles() == []

    def test_get_profile_nonexistent(self):
        service = make_service()
        profile = service.get_profile("nonexistent")
        assert profile is None

    def test_get_profile_default_fallback(self):
        service = make_service()
        service._profiles["default"] = LLMProfile(
            name="default",
            base_url="http://localhost:11434/v1",
            api_key="",
            model_id="llama3",
            prompt_template="{pairs}",
        )
        profile = service.get_profile()
        assert profile is not None
        assert profile.name == "default"

    def test_resolve_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("TEST_LLM_KEY", "resolved-key")
        profile = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="${TEST_LLM_KEY}",
            model_id="gpt-4o-mini",
            prompt_template="",
        )
        assert profile.api_key.get_secret_value() == "resolved-key"

    def test_resolve_api_key_literal(self):
        profile = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="sk-literal-key",
            model_id="gpt-4o-mini",
            prompt_template="",
        )
        assert profile.api_key.get_secret_value() == "sk-literal-key"

    @pytest.mark.asyncio
    @patch("core.llm_decipher.AsyncOpenAI")
    async def test_call_llm_success(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        msg = MagicMock()
        msg.message.content = '{"intent": "turn_on", "confidence": 0.95}'
        choice = MagicMock()
        choice.choices = [msg]
        mock_client.chat.completions.create = AsyncMock(return_value=choice)

        service = make_service()
        profile = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            prompt_template="{pairs}",
        )
        result = await service.call_llm(profile, "test prompt")

        assert result["success"] is True
        assert "intent" in result["content"]

    @pytest.mark.asyncio
    @patch("core.llm_decipher.AsyncOpenAI")
    async def test_call_llm_retry_then_fail(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Server Error"))

        service = make_service()
        profile = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            prompt_template="{pairs}",
            max_retries=1,
        )
        result = await service.call_llm(profile, "test prompt")

        assert result["success"] is False
        assert result["error"] is not None

    @pytest.mark.asyncio
    @patch("core.llm_decipher.AsyncOpenAI")
    async def test_call_llm_timeout(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("request timeout exceeded"))

        service = make_service()
        profile = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            prompt_template="{pairs}",
            max_retries=1,
            timeout=5,
        )
        result = await service.call_llm(profile, "test prompt")
        assert result["success"] is False
        assert "timeout" in result["error"].lower()

    def _make_client(mock_client_class, content: str):
        """Wire a fake OpenAI completion returning ``content``."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        msg = MagicMock()
        msg.message.content = content
        choice = MagicMock()
        choice.choices = [msg]
        mock_client.chat.completions.create = AsyncMock(return_value=choice)
        return mock_client

    @pytest.mark.asyncio
    @patch("core.llm_decipher.AsyncOpenAI")
    async def test_api_key_passed_to_sdk(self, mock_client_class):
        """The real API key must be forwarded to the SDK (regression for the
        literal ``Authorization: ******`` bug)."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        msg = MagicMock()
        msg.message.content = '{"intent": "x", "fields": {}, "confidence": 0.5}'
        choice = MagicMock()
        choice.choices = [msg]
        mock_client.chat.completions.create = AsyncMock(return_value=choice)

        service = make_service()
        profile = LLMProfile(
            name="default",
            base_url="https://api.openai.com/v1",
            api_key="sk-super-secret",
            model_id="gpt-4o",
            prompt_template="{pairs}",
        )
        await service.call_llm(profile, "prompt")

        mock_client_class.assert_called_once()
        _, kwargs = mock_client_class.call_args
        assert kwargs["api_key"] == "sk-super-secret"
        assert kwargs["base_url"] == "https://api.openai.com/v1"

    def test_json_extraction_from_markdown(self):
        """Test extracting JSON from ```json blocks via _parse_llm_json."""
        content = """Here is the analysis:
```json
{"intent": "turn_on", "fields": {"state": "on"}, "confidence": 0.95}
```
End."""
        parsed = _parse_llm_json(content)
        assert parsed is not None
        assert parsed["intent"] == "turn_on"
        assert parsed["confidence"] == 0.95  # noqa: PLR2004

    def test_json_extraction_from_bare_code_block(self):
        content = """Response:
```
{"intent": "turn_off", "fields": {}, "confidence": 0.8}
```"""
        parsed = _parse_llm_json(content)
        assert parsed is not None
        assert parsed["intent"] == "turn_off"

    def test_json_parse_repairs_malformed(self):
        """A slightly damaged payload is still recovered."""
        parsed = _parse_llm_json('{"intent": "turn_on", "confidence": 0.9,}')
        assert parsed is not None
        assert parsed["intent"] == "turn_on"

    def test_decipher_result_creation(self):
        result = DecipherResult(
            pair_id="pair-001",
            device_id="device-001",
            vendor="shelly",
            intent="turn_on",
            fields={"state": "on"},
            confidence=0.95,
            success=True,
        )
        assert result.pair_id == "pair-001"
        assert result.intent == "turn_on"
        assert result.success is True
        assert result.error is None

    def test_decipher_result_error(self):
        result = DecipherResult(
            pair_id="pair-002",
            device_id="device-002",
            vendor="unknown",
            intent="unknown",
            fields={},
            confidence=0.0,
            success=False,
            error="API error",
        )
        assert result.success is False
        assert result.error == "API error"

    @pytest.mark.asyncio
    async def test_decipher_batch_empty(self):
        service = make_service()
        results = await service.decipher_batch([])
        assert results == []

    def test_get_db_schema_format(self):
        service = make_service()
        schema = service._get_db_schema("shelly")
        assert "Vendor: shelly" in schema
        assert "devices" in schema

    def test_get_recent_patterns_returns_empty(self):
        service = make_service()
        patterns = service._get_recent_patterns("shelly", "ac")
        assert patterns == []

    async def test_decipher_with_params_profile_not_found(self):
        service = make_service()
        result = await service.decipher_with_params(
            {"pairs": [], "vendor": "test", "device_type": "ac"},
            profile_name="nonexistent",
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @patch("core.llm_decipher.AsyncOpenAI")
    async def test_decipher_with_params_success(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        msg = MagicMock()
        msg.message.content = (
            '{"intent": "turn_on", "fields": {"state": "on"}, "confidence": 0.95}'
        )
        choice = MagicMock()
        choice.choices = [msg]
        mock_client.chat.completions.create = AsyncMock(return_value=choice)

        service = make_service()
        service._profiles["test"] = LLMProfile(
            name="test",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model_id="gpt-4o",
            prompt_template="Analyze {vendor} {device_type} {pairs} {device_id}",
        )
        result = await service.decipher_with_params(
            {
                "pairs": [{"req": "test"}],
                "vendor": "shelly",
                "device_type": "ac",
                "device_id": "d1",
            },
            profile_name="test",
        )
        assert result["success"] is True
        assert result["analysis"]["intent"] == "turn_on"
