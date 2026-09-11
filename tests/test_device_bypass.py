"""
Tests for the per-IP bypass mode.

Covers the ``ip_profiles[ip].bypass`` config flag and its runtime effect:
a bypassed device's traffic still transits the gateway but is forwarded
straight to the cloud without pipeline analysis (no buffer/LLM/local match).

- :meth:`core.config.IpProfileConfig.bypass` default.
- :meth:`core.database.DatabaseManager.is_ips_bypassed`.
- :meth:`core.config.ConfigManager.set_ip_profile_bypass` persistence.
"""

import pytest
import pytest_asyncio

from core.config import ConfigManager, IpProfileConfig
from core.database import DatabaseManager


class TestIpProfileConfigBypass:
    def test_bypass_defaults_false(self):
        assert IpProfileConfig().bypass is False

    def test_bypass_parses(self):
        assert IpProfileConfig(bypass=True).bypass is True


@pytest_asyncio.fixture
async def dm(tmp_path):
    core_db_path = tmp_path / "core.db"
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{core_db_path}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await manager.initialize()
    yield manager
    await manager.close()


class _FakeConfig:
    """Minimal config-shaped object exposing core.ip_profiles."""

    class _Core:
        ip_profiles = {}

    core = _Core()


class TestIsIpsBypassed:
    @pytest.mark.asyncio
    async def test_unknown_ip_returns_false(self, dm, monkeypatch):
        _FakeConfig._Core.ip_profiles = {}
        monkeypatch.setattr("core.database.get_config", _FakeConfig)
        assert await dm.is_ips_bypassed("192.168.1.50") is False

    @pytest.mark.asyncio
    async def test_known_ip_not_bypassed_returns_false(self, dm, monkeypatch):
        _FakeConfig._Core.ip_profiles = {"192.168.1.50": IpProfileConfig()}
        monkeypatch.setattr("core.database.get_config", _FakeConfig)
        assert await dm.is_ips_bypassed("192.168.1.50") is False

    @pytest.mark.asyncio
    async def test_known_ip_bypassed_returns_true(self, dm, monkeypatch):
        _FakeConfig._Core.ip_profiles = {
            "192.168.1.50": IpProfileConfig(bypass=True)
        }
        monkeypatch.setattr("core.database.get_config", _FakeConfig)
        assert await dm.is_ips_bypassed("192.168.1.50") is True


class TestConfigManagerPersistBypass:
    def test_sets_and_persists_bypass(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("core:\n  ip_profiles: {}\n", encoding="utf-8")
        manager = ConfigManager(cfg_path)
        assert manager.set_ip_profile_bypass("192.168.1.50", True) is True
        # Re-load from disk to prove durability.
        reloaded = ConfigManager(cfg_path)
        assert reloaded.config.core.ip_profiles["192.168.1.50"].bypass is True

    def test_clears_bypass(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("core:\n  ip_profiles: {}\n", encoding="utf-8")
        manager = ConfigManager(cfg_path)
        assert manager.set_ip_profile_bypass("192.168.1.50", True) is True
        assert manager.set_ip_profile_bypass("192.168.1.50", False) is True
        reloaded = ConfigManager(cfg_path)
        assert reloaded.config.core.ip_profiles["192.168.1.50"].bypass is False
