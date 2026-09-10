"""Tests for dashboard-managed LLM settings (which LLM to use + configuration)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.llm_decipher import LLMDecipherService
from core.server import app


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


def _profile_cfg(base_url="http://a/v1", api_key="k"):
    return MockConfig(
        base_url=base_url,
        api_key=api_key,
        model_id="m",
        prompt_template="",
        enabled=True,
        timeout=30,
        max_retries=2,
    )


def _service_with_profiles():
    cm = MockConfigManager()
    cm.config = MockConfig(
        llm_decipher=MockConfig(
            enabled=True,
            default_profile="default",
            profiles={"default": _profile_cfg()},
        )
    )
    return LLMDecipherService(config_manager=cm), cm


class TestLLMSettingsService:
    def test_get_settings_returns_effective_state(self):
        service, _ = _service_with_profiles()
        settings = service.get_settings()
        assert settings["enabled"] is True
        assert settings["default_profile"] == "default"
        assert "default" in settings["profiles"]
        assert settings["profiles"]["default"]["base_url"] == "http://a/v1"
        assert settings["profiles"]["default"]["api_key"] == "k"

    def test_list_profiles_gated_by_enabled(self):
        service, _ = _service_with_profiles()
        assert service.list_profiles() == ["default"]
        service._enabled = False
        assert service.list_profiles() == []
        assert service.available_profiles() == []

    def test_update_settings_persists_and_reloads(self):
        service, _ = _service_with_profiles()
        with patch("core.llm_decipher.save_llm_settings") as mock_save, patch(
            "core.llm_decipher.load_llm_settings", return_value=None
        ):
            updated = service.update_settings(
                {
                    "enabled": True,
                    "default_profile": "default",
                    "profiles": {
                        "default": {
                            "base_url": "http://localhost:11434/v1",
                            "api_key": "ollama",
                            "model_id": "llama3.1:8b",
                            "prompt_template": "Analyze {pairs}",
                            "enabled": True,
                            "timeout": 60,
                            "max_retries": 3,
                        }
                    },
                }
            )
        # The payload is persisted with the validated values.
        payload = mock_save.call_args[0][0]
        assert payload["default_profile"] == "default"
        assert payload["profiles"]["default"]["timeout"] == 60
        assert payload["profiles"]["default"]["max_retries"] == 3
        assert updated["default_profile"] == "default"

    def test_update_settings_rejects_unknown_default(self):
        service, _ = _service_with_profiles()
        with patch("core.llm_decipher.save_llm_settings") as mock_save:
            with pytest.raises(ValueError, match="not a known profile"):
                service.update_settings(
                    {
                        "enabled": True,
                        "default_profile": "missing",
                        "profiles": {"default": {"base_url": "x", "model_id": "m"}},
                    }
                )
        mock_save.assert_not_called()

    def test_runtime_profiles_replace_config_yaml(self):
        """A profile deleted in the dashboard must disappear from the effective set."""
        service, _ = _service_with_profiles()
        # Runtime file only keeps the 'local' profile; the config.yaml 'default'
        # must be dropped because the runtime file is the source of truth.
        with patch(
            "core.llm_decipher.load_llm_settings",
            return_value={
                "enabled": True,
                "default_profile": "local",
                "profiles": {
                    "local": {
                        "base_url": "http://localhost:11434/v1",
                        "api_key": "ollama",
                        "model_id": "qwen2.5:7b",
                        "prompt_template": "x",
                        "enabled": True,
                        "timeout": 30,
                        "max_retries": 2,
                    }
                },
            },
        ):
            service._load_config()
        assert "default" not in service._profiles
        assert "local" in service._profiles
        assert service._default_profile == "local"

    def test_update_settings_allows_empty_default_when_no_profiles(self):
        service, _ = _service_with_profiles()
        # Simulate the real save-then-load round-trip: the runtime file holds
        # the payload that was just saved, so the reload applies it.
        saved: dict = {}

        def fake_save(payload):
            saved.clear()
            saved.update(payload)

        with patch("core.llm_decipher.save_llm_settings", side_effect=fake_save), patch(
            "core.llm_decipher.load_llm_settings", side_effect=lambda: saved or None
        ):
            updated = service.update_settings(
                {"enabled": True, "default_profile": "", "profiles": {}}
            )
        assert updated["default_profile"] == ""
        assert updated["profiles"] == {}

    def test_update_settings_rejects_non_object_profiles(self):
        service, _ = _service_with_profiles()
        with pytest.raises(ValueError, match="profiles must be an object"):
            service.update_settings({"enabled": True, "profiles": []})

    def test_update_settings_rejects_non_object_profile(self):
        service, _ = _service_with_profiles()
        with pytest.raises(ValueError, match="must be an object"):
            service.update_settings(
                {"enabled": True, "default_profile": "default", "profiles": {"default": "x"}}
            )


class TestLLMSettingsAPI:
    def _client(self):
        # Use a known admin key so the control-plane auth passes.
        from core.server import config_manager

        config_manager.config.security.admin_api_key = "test-admin-key"
        config_manager.config.security.readonly_api_key = "test-ro-key"
        return TestClient(app)

    def _mock_service(self):
        svc = MagicMock()
        svc.get_settings.return_value = {
            "enabled": True,
            "default_profile": "default",
            "profiles": {
                "default": {
                    "base_url": "http://a/v1",
                    "api_key": "k",
                    "model_id": "m",
                    "prompt_template": "",
                    "enabled": True,
                    "timeout": 30,
                    "max_retries": 2,
                }
            },
        }
        svc.update_settings.return_value = {
            "enabled": True,
            "default_profile": "default",
            "profiles": {},
        }
        return svc

    def test_get_settings(self):
        import core.server as server

        server.llm_decipher_service = self._mock_service()
        client = self._client()
        resp = client.get("/api/llm/settings", headers={"X-API-Key": "test-admin-key"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["default_profile"] == "default"
        assert "default" in body["profiles"]

    def test_put_settings(self):
        import core.server as server

        svc = self._mock_service()
        server.llm_decipher_service = svc
        client = self._client()
        resp = client.put(
            "/api/llm/settings",
            headers={"X-API-Key": "test-admin-key"},
            json={"enabled": True, "default_profile": "default", "profiles": {}},
        )
        assert resp.status_code == 200
        svc.update_settings.assert_called_once()

    def test_put_settings_invalid_body(self):
        import core.server as server

        server.llm_decipher_service = self._mock_service()
        client = self._client()
        resp = client.put(
            "/api/llm/settings",
            headers={"X-API-Key": "test-admin-key"},
            json=["not", "an", "object"],
        )
        assert resp.status_code == 400

    def test_post_profile(self):
        import core.server as server

        svc = self._mock_service()
        server.llm_decipher_service = svc
        client = self._client()
        resp = client.post(
            "/api/llm/settings/profiles",
            headers={"X-API-Key": "test-admin-key"},
            json={"name": "local", "base_url": "http://x", "model_id": "m"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "local"
        svc.update_settings.assert_called_once()

    def test_post_profile_requires_name(self):
        import core.server as server

        server.llm_decipher_service = self._mock_service()
        client = self._client()
        resp = client.post(
            "/api/llm/settings/profiles",
            headers={"X-API-Key": "test-admin-key"},
            json={"base_url": "http://x"},
        )
        assert resp.status_code == 400

    def test_delete_profile(self):
        import core.server as server

        svc = self._mock_service()
        server.llm_decipher_service = svc
        client = self._client()
        resp = client.delete(
            "/api/llm/settings/profiles/default",
            headers={"X-API-Key": "test-admin-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "default"
        svc.update_settings.assert_called_once()

    def test_delete_missing_profile(self):
        import core.server as server

        svc = self._mock_service()
        server.llm_decipher_service = svc
        client = self._client()
        resp = client.delete(
            "/api/llm/settings/profiles/nope",
            headers={"X-API-Key": "test-admin-key"},
        )
        assert resp.status_code == 404
