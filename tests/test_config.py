"""
Tests for the Configuration module (Pydantic models, ConfigManager, hot-reload).
"""

from unittest.mock import MagicMock

import pytest
import yaml

from core.config import (
    Config,
    ConfigManager,
    CoreConfig,
    DNSConfig,
    LearningConfig,
    LLMDecipherConfig,
    LLMDecipherProfile,
    ModificationAction,
    ModificationConfig,
    ModificationRule,
    ProxyConfig,
    TLSConfig,
    TrafficSelectionConfig,
    get_config_manager,
)

_EXPECTED_DEFAULT_CONTEXT_BUFFER_SIZE = 524288
_CUSTOM_CONTEXT_BUFFER_SIZE = 1048576
_EXPECTED_PROXY_PORT = 8911
_EXPECTED_FALLBACK_TIMEOUT = 10
_EXPECTED_FALLBACK_CONFIDENCE = 0.7
_EXPECTED_REQUEST_TIMEOUT = 60
_EXPECTED_MAX_REQUEST_SIZE = 2097152
_EXPECTED_MATCH_THRESHOLD = 0.85
_CUSTOM_MATCH_THRESHOLD = 0.9
_CUSTOM_MIN_PATTERNS = 25
_CUSTOM_PROXY_PORT = 8080
_EXPECTED_TLS_DECRYPT_PORT = 8883

# ---------------------------------------------------------------------------
#  Individual model defaults
# ---------------------------------------------------------------------------


class TestCoreConfig:
    def test_defaults(self):
        c = CoreConfig()
        assert c.database_url == "sqlite+aiosqlite:///./ridebase/core.db"
        assert c.device_db_dir == "./ridebase/devices"
        assert c.device_databases == {}
        assert c.default_context_buffer_size == _EXPECTED_DEFAULT_CONTEXT_BUFFER_SIZE

    def test_custom(self):
        c = CoreConfig(
            database_url="postgresql:///custom",
            device_db_dir="/tmp/devices",
            default_context_buffer_size=1048576,
        )
        assert c.database_url == "postgresql:///custom"
        assert c.device_db_dir == "/tmp/devices"
        assert c.default_context_buffer_size == _CUSTOM_CONTEXT_BUFFER_SIZE


class TestTLSConfig:
    def test_defaults(self):
        t = TLSConfig()
        assert t.enabled is True
        assert t.cert_file == "./certs/ride-api.pem"
        assert t.key_file == "./certs/ride-api.key"

    def test_custom(self):
        t = TLSConfig(enabled=False, cert_file="/tmp/cert.pem")
        assert t.enabled is False
        assert t.key_file == "./certs/ride-api.key"  # unchanged


class TestProxyConfig:
    def test_defaults(self):
        p = ProxyConfig()
        assert p.host == "0.0.0.0"
        assert p.port == _EXPECTED_PROXY_PORT

    def test_nested_tls(self):
        p = ProxyConfig(port=9090)
        assert p.tls.enabled is True
        assert p.tls.cert_file == "./certs/ride-api.pem"

    def test_fallback_defaults(self):
        p = ProxyConfig()
        assert p.fallback.enabled is True
        assert p.fallback.timeout == _EXPECTED_FALLBACK_TIMEOUT
        assert p.fallback.confidence_threshold == _EXPECTED_FALLBACK_CONFIDENCE

    def test_request_timeout(self):
        p = ProxyConfig(request_timeout=60, max_request_size=2097152)
        assert p.request_timeout == _EXPECTED_REQUEST_TIMEOUT
        assert p.max_request_size == _EXPECTED_MAX_REQUEST_SIZE


class TestDNSConfig:
    def test_defaults(self):
        d = DNSConfig()
        assert d.pihole_custom_dns == ""
        assert d.adguard_rewrites == ""

    def test_custom(self):
        d = DNSConfig(pihole_custom_dns="192.168.1.10")
        assert d.pihole_custom_dns == "192.168.1.10"


class TestLearningConfig:
    def test_defaults(self):
        lc = LearningConfig()
        assert lc.enabled is True
        assert lc.default_mode == "learning"
        assert lc.default_match_threshold == _EXPECTED_MATCH_THRESHOLD
        assert lc.production_no_fallback is False
        assert lc.signal_forward_to_cloud is False

    def test_custom(self):
        lc = LearningConfig(
            enabled=False,
            default_mode="production",
            default_match_threshold=0.9,
            min_patterns_for_production=25,
        )
        assert lc.enabled is False
        assert lc.default_mode == "production"
        assert lc.default_match_threshold == _CUSTOM_MATCH_THRESHOLD
        assert lc.min_patterns_for_production == _CUSTOM_MIN_PATTERNS


class TestLLMDecipherProfile:
    def test_basic(self):
        p = LLMDecipherProfile(
            base_url="http://localhost:11434/v1",
            api_key="sk-test",
            model_id="llama3",
            prompt_template="Analyze: {pairs}",
        )
        assert p.base_url == "http://localhost:11434/v1"
        assert p.model_id == "llama3"
        assert p.prompt_template == "Analyze: {pairs}"

    def test_empty_defaults(self):
        p = LLMDecipherProfile()
        assert p.base_url == ""
        assert p.api_key == ""
        assert p.model_id == ""
        assert p.prompt_template == ""


class TestLLMDecipherConfig:
    def test_defaults(self):
        llm = LLMDecipherConfig()
        assert llm.enabled is True
        assert llm.default_profile == "default"
        assert llm.profiles == {}

    def test_with_profiles(self):
        llm = LLMDecipherConfig(
            default_profile="ollama",
            profiles={
                "ollama": LLMDecipherProfile(
                    base_url="http://localhost:11434/v1",
                    api_key="",
                    model_id="llama3",
                    prompt_template="X",
                ),
            },
        )
        assert llm.default_profile == "ollama"
        assert len(llm.profiles) == 1
        assert llm.profiles["ollama"].model_id == "llama3"


class TestModificationRule:
    def test_defaults(self):
        r = ModificationRule()
        assert r.name == ""
        assert r.action == ModificationAction.modify
        assert r.enabled is True
        assert r.priority == 0

    def test_block_action(self):
        r = ModificationRule(name="block-test", action=ModificationAction.block)
        assert r.action is ModificationAction.block

    def test_custom(self):
        r = ModificationRule(
            name="redirect-google",
            match_type="hostname",
            match_value="google.com",
            action=ModificationAction.redirect,
            target_value="http://localhost:8080",
            priority=10,
        )
        assert r.action is ModificationAction.redirect
        assert r.target_value == "http://localhost:8080"


class TestModificationConfig:
    def test_defaults(self):
        m = ModificationConfig()
        assert m.enabled is True
        assert m.rules == []

    def test_with_rules(self):
        r = ModificationRule(name="test", target_field="body", target_value='{"ok": true}')
        m = ModificationConfig(enabled=False, rules=[r])
        assert m.enabled is False
        assert len(m.rules) == 1


class TestTrafficSelectionConfig:
    def test_defaults(self):
        t = TrafficSelectionConfig()
        assert t.default_action == "intercept"
        assert t.rules == []


# ---------------------------------------------------------------------------
#  Enum values
# ---------------------------------------------------------------------------


class TestModificationAction:
    def test_members(self):
        assert ModificationAction.modify.value == "modify"
        assert ModificationAction.block.value == "block"
        assert ModificationAction.inject.value == "inject"
        assert ModificationAction.replace.value == "replace"
        assert ModificationAction.redirect.value == "redirect"
        assert ModificationAction.delay.value == "delay"


# ---------------------------------------------------------------------------
#  Root Config
# ---------------------------------------------------------------------------


class TestRootConfig:
    def test_defaults(self):
        c = Config()
        assert c.core.database_url == "sqlite+aiosqlite:///./ridebase/core.db"
        assert c.proxy.port == _EXPECTED_PROXY_PORT
        assert c.learning.enabled is True
        assert c.llm_decipher.enabled is True
        assert c.observability.logging.level == "INFO"
        assert c.traffic_selection.default_action == "intercept"

    def test_custom_vendor(self):
        c = Config(
            vendors={"shelly": {"enabled": True, "cloud": {"api_endpoint": "https://shelly.cloud"}}}
        )
        assert "shelly" in c.vendors
        assert c.vendors["shelly"].cloud.api_endpoint == "https://shelly.cloud"

    def test_yaml_round_trip(self, tmp_path):  # noqa: ARG002
        data = {
            "core": {"database_url": "sqlite:///data/core.db", "device_db_dir": "/data/devices"},
            "proxy": {"port": 8080},
            "learning": {"enabled": False, "default_mode": "production"},
            "vendors": {"shelly": {"enabled": True}},
        }
        c = Config(**data)
        assert c.core.database_url == "sqlite:///data/core.db"
        assert c.proxy.port == _CUSTOM_PROXY_PORT
        assert c.learning.default_mode == "production"
        assert c.learning.enabled is False
        assert c.vendors["shelly"].enabled is True

    def test_llm_profile_from_dict(self):
        c = Config(
            **{
                "llm_decipher": {
                    "default_profile": "openai",
                    "profiles": {
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key": "${OPENAI_API_KEY}",
                            "model_id": "gpt-4",
                            "prompt_template": "Analyze: {pairs}",
                        }
                    },
                }
            }
        )
        assert c.llm_decipher.default_profile == "openai"
        assert c.llm_decipher.profiles["openai"].model_id == "gpt-4"

    def test_protocol_server_defaults(self):
        c = Config()
        assert c.protocol_servers.mqtt.enabled is False
        assert c.protocol_servers.coap.enabled is False
        assert c.protocol_servers.modbus.enabled is False
        assert c.protocol_servers.websocket.enabled is False
        assert c.protocol_servers.zigbee_bridge.enabled is False
        assert c.protocol_servers.zwave_bridge.enabled is False
        assert c.protocol_servers.matter_bridge.enabled is False

    def test_tls_decrypt_defaults(self):
        c = Config()
        assert c.tls_decrypt.enabled is False
        assert _EXPECTED_TLS_DECRYPT_PORT in c.tls_decrypt.listen_ports
        assert c.tls_decrypt.ca_cert_path == "./certs/ca.pem"
        assert c.tls_decrypt.pinning_bypass == {}


# ---------------------------------------------------------------------------
#  ConfigManager
# ---------------------------------------------------------------------------


class TestConfigManager:
    def test_init_with_default_path(self):
        cm = ConfigManager()
        # On Windows, Path normalizes "/" to "\" so compare with path parts
        parts = list(cm.config_path.parts)
        assert "config" in parts
        assert "config.yaml" in parts[-1]

    def test_init_with_custom_path(self, tmp_path):
        path = tmp_path / "test.yaml"
        cm = ConfigManager(config_path=path)
        assert cm.config_path == path

    def test_load_raises_on_missing_file(self, tmp_path):
        cm = ConfigManager(config_path=tmp_path / "nonexistent.yaml")
        with pytest.raises(FileNotFoundError):
            cm.load()

    def test_load_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        data = {
            "core": {"database_url": "sqlite:///data/core.db"},
            "learning": {"enabled": False, "default_mode": "production"},
        }
        with path.open("w") as f:
            yaml.dump(data, f)

        cm = ConfigManager(config_path=path)
        config = cm.load()
        assert config.core.database_url == "sqlite:///data/core.db"
        assert config.learning.enabled is False
        assert config.learning.default_mode == "production"

    def test_load_complete_yaml(self, tmp_path):
        path = tmp_path / "full.yaml"
        data = {
            "core": {"database_url": "sqlite:///full.db"},
            "vendor": {"shelly": {"enabled": True}},
            "llm_decipher": {
                "default_profile": "local",
                "profiles": {
                    "local": {
                        "base_url": "http://localhost:11434/v1",
                        "api_key": "",
                        "model_id": "llama3",
                        "prompt_template": "Analyze: {pairs}",
                    }
                },
            },
        }
        with path.open("w") as f:
            yaml.dump(data, f)

        cm = ConfigManager(config_path=path)
        config = cm.load()
        assert config.core.database_url == "sqlite:///full.db"

    def test_config_property_loads_on_first_access(self, tmp_path):
        path = tmp_path / "config.yaml"
        data = {"core": {"database_url": "sqlite:///access.db"}}
        with path.open("w") as f:
            yaml.dump(data, f)

        cm = ConfigManager(config_path=path)
        c = cm.config
        assert c.core.database_url == "sqlite:///access.db"

    def test_config_property_caches(self, tmp_path):
        path = tmp_path / "config.yaml"
        data = {"core": {"database_url": "sqlite:///cache.db"}}
        with path.open("w") as f:
            yaml.dump(data, f)

        cm = ConfigManager(config_path=path)
        c1 = cm.config
        c2 = cm.config
        assert c1 is c2

    def test_get_vendor_config(self, tmp_path):
        path = tmp_path / "config.yaml"
        data = {
            "vendors": {
                "shelly": {"enabled": True, "cloud": {"api_endpoint": "https://shelly.cloud"}}
            }
        }
        with path.open("w") as f:
            yaml.dump(data, f)

        cm = ConfigManager(config_path=path)
        vc = cm.get_vendor_config("shelly")
        assert vc is not None
        assert vc.enabled is True
        assert vc.cloud.api_endpoint == "https://shelly.cloud"
        assert cm.get_vendor_config("unknown") is None

    def test_register_callback(self, tmp_path):
        path = tmp_path / "config.yaml"
        with path.open("w") as f:
            yaml.dump({"core": {}}, f)

        cm = ConfigManager(config_path=path)
        callback = MagicMock()
        cm.register_callback(callback)
        assert callback in cm._callbacks

    def test_start_and_stop_watching(self, tmp_path):
        path = tmp_path / "config.yaml"
        with path.open("w") as f:
            yaml.dump({"core": {}}, f)

        cm = ConfigManager(config_path=path)
        cm.start_watching()
        assert cm._watch_thread is not None
        assert cm._watch_thread.is_alive()

        cm.stop_watching()
        cm._watch_thread.join(timeout=5)
        assert not cm._watch_thread.is_alive()

    def test_start_watching_twice_is_idempotent(self, tmp_path):
        path = tmp_path / "config.yaml"
        with path.open("w") as f:
            yaml.dump({"core": {}}, f)

        cm = ConfigManager(config_path=path)
        cm.start_watching()
        thread = cm._watch_thread
        cm.start_watching()
        assert cm._watch_thread is thread

        cm.stop_watching()


# ---------------------------------------------------------------------------
#  Global helpers
# ---------------------------------------------------------------------------


class TestGlobalHelpers:
    def test_get_config_manager_singleton(self):
        cm1 = get_config_manager()
        cm2 = get_config_manager()
        assert cm1 is cm2

    def test_get_config_manager_with_path_ignored_when_already_created(self):
        """The singleton ignores a new path once the global instance exists."""
        cm = get_config_manager()
        original_path = cm.config_path
        cm2 = get_config_manager(config_path="/some/other/path.yaml")
        assert cm2 is cm
        assert cm2.config_path == original_path
