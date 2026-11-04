"""Secrets never leave through the API in clear, and are not rewritten (S-11)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core import server
from core.config import SECRET_MASK, mask_secrets, restore_masked_secrets
from core.server import app, config_manager
from tests.test_llm_settings import MockConfig, MockConfigManager, _profile_cfg

HEADERS = {"X-API-Key": "test-password"}


def _dump():
    return {
        "security": {"password": "pw-1", "auth_enabled": True},
        "llm_decipher": {
            "profiles": {
                "literal": {"api_key": "sk-literal", "max_tokens": 512},
                "env": {"api_key": "${OPENAI_API_KEY}"},
                "empty": {"api_key": ""},
            }
        },
        "protocol_servers": {"zigbee_bridge": {"mqtt_pass": "zz", "mqtt_user": "u"}},
        "tls_decrypt": {"ca_key_path": "./certs/ca.key"},
        "proxy": {"key_file": "./certs/ride-api.key"},
        "ip_profiles": {"10.0.0.5": {"bypass": True}},
        "adapters": {"vendor": {"extra": {"token": "t-1", "region": "eu"}}},
    }


def test_mask_secrets_masks_only_stored_secrets():
    masked = mask_secrets(_dump())
    assert masked["security"]["password"] == SECRET_MASK
    assert masked["llm_decipher"]["profiles"]["literal"]["api_key"] == SECRET_MASK
    # An env reference is a pointer, not the secret: it stays visible.
    assert masked["llm_decipher"]["profiles"]["env"]["api_key"] == "${OPENAI_API_KEY}"
    assert masked["llm_decipher"]["profiles"]["empty"]["api_key"] == ""
    assert masked["protocol_servers"]["zigbee_bridge"]["mqtt_pass"] == SECRET_MASK
    assert masked["adapters"]["vendor"]["extra"]["token"] == SECRET_MASK
    # Paths, flags and ordinary settings are untouched.
    assert masked["tls_decrypt"]["ca_key_path"] == "./certs/ca.key"
    assert masked["proxy"]["key_file"] == "./certs/ride-api.key"
    assert masked["ip_profiles"]["10.0.0.5"]["bypass"] is True
    assert masked["llm_decipher"]["profiles"]["literal"]["max_tokens"] == 512  # noqa: PLR2004
    assert masked["protocol_servers"]["zigbee_bridge"]["mqtt_user"] == "u"
    assert masked["adapters"]["vendor"]["extra"]["region"] == "eu"


def test_restore_masked_secrets_keeps_stored_values():
    current = _dump()
    incoming = mask_secrets(current)
    incoming["adapters"]["vendor"]["extra"]["region"] = "us"  # an unrelated edit
    incoming["llm_decipher"]["profiles"]["literal"]["api_key"] = "sk-new"  # a real change

    restored = restore_masked_secrets(incoming, current)
    assert restored["security"]["password"] == "pw-1"
    assert restored["protocol_servers"]["zigbee_bridge"]["mqtt_pass"] == "zz"
    assert restored["adapters"]["vendor"]["extra"]["token"] == "t-1"
    assert restored["adapters"]["vendor"]["extra"]["region"] == "us"
    assert restored["llm_decipher"]["profiles"]["literal"]["api_key"] == "sk-new"


def _env_profile_service(monkeypatch):
    from core.llm_decipher import LLMDecipherService  # noqa: PLC0415

    monkeypatch.setenv("RTA_TEST_LLM_KEY", "sk-from-env")
    cm = MockConfigManager()
    cm.config = MockConfig(
        llm_decipher=MockConfig(
            enabled=True,
            default_profile="default",
            profiles={"default": _profile_cfg(api_key="${RTA_TEST_LLM_KEY}")},
        )
    )
    with patch("core.llm_decipher.load_llm_settings", return_value=None):
        return LLMDecipherService(config_manager=cm)


def test_llm_settings_show_env_reference_not_the_key(monkeypatch):
    service = _env_profile_service(monkeypatch)
    assert service.get_settings()["profiles"]["default"]["api_key"] == "${RTA_TEST_LLM_KEY}"
    real = service.get_settings(include_secrets=True)
    assert real["profiles"]["default"]["api_key"] == "sk-from-env"
    persisted = service.get_settings_for_persistence()
    assert persisted["profiles"]["default"]["api_key"] == "${RTA_TEST_LLM_KEY}"
    assert service.stored_api_key("default") == "sk-from-env"


def test_saving_masked_key_keeps_the_env_reference(monkeypatch):
    """Round-tripping the dashboard form must not persist the resolved env key."""
    service = _env_profile_service(monkeypatch)
    form = service.get_settings()
    form["profiles"]["default"]["api_key"] = SECRET_MASK  # unchanged masked field

    with (
        patch("core.llm_decipher.save_llm_settings") as save,
        patch("core.llm_decipher.load_llm_settings", return_value=None),
    ):
        service.update_settings(form)
    saved = save.call_args.args[0]
    assert saved["profiles"]["default"]["api_key"] == "${RTA_TEST_LLM_KEY}"
    assert "sk-from-env" not in str(saved)


def _client():
    config_manager.config.security.password = "test-password"
    return TestClient(app)


def test_get_config_does_not_return_the_password():
    client = _client()
    resp = client.get("/api/config", headers=HEADERS)
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["config"]["security"]["password"] == SECRET_MASK
    assert "test-password" not in resp.text


def test_put_config_with_masked_password_keeps_it():
    svc = MagicMock()
    svc.get_settings_for_persistence.return_value = {
        "enabled": True,
        "default_profile": "",
        "profiles": {},
    }
    server.llm_decipher_service = svc
    client = _client()
    cfg = client.get("/api/config", headers=HEADERS).json()["config"]

    def pretend_write(candidate):
        config_manager._config = candidate
        return True

    with patch.object(config_manager, "_write", side_effect=pretend_write):
        resp = client.put("/api/config", headers=HEADERS, json={"config": cfg})
    assert resp.status_code == 200  # noqa: PLR2004
    assert config_manager.config.security.password == "test-password"
    assert resp.json()["config"]["security"]["password"] == SECRET_MASK


def test_put_config_invalid_value_is_a_400_without_echoing_input():
    client = _client()
    cfg = client.get("/api/config", headers=HEADERS).json()["config"]
    cfg["proxy"]["port"] = "not-a-port"
    resp = client.put("/api/config", headers=HEADERS, json={"config": cfg})
    assert resp.status_code == 400  # noqa: PLR2004
    assert "not-a-port" not in resp.text
    assert any("port" in d["loc"] for d in resp.json()["details"])


def test_connection_test_with_masked_key_uses_stored_key():
    svc = MagicMock()
    svc.stored_api_key.return_value = "real-key"
    svc.test_connection = AsyncMock(return_value={"success": True})
    server.llm_decipher_service = svc
    client = _client()
    resp = client.post(
        "/api/llm/profiles/test",
        headers=HEADERS,
        json={
            "name": "default",
            "base_url": "http://a/v1",
            "model_id": "m",
            "api_key": SECRET_MASK,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    svc.stored_api_key.assert_called_once_with("default")
    assert svc.test_connection.call_args.kwargs["api_key"] == "real-key"
