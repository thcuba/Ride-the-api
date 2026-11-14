"""Unit tests verifying the modularized core.database architecture and backward compatibility."""

from __future__ import annotations

import pytest
import pytest_asyncio

import core.database as db_pkg
import core.database.device_pool as pool_mod
import core.database.engine as engine_mod
import core.database.manager as manager_mod
from core.database import (
    Base,
    DatabaseManager,
    DeviceDatabasePool,
    create_configured_engine,
    get_db_manager,
    init_db_manager,
)


@pytest_asyncio.fixture
async def temp_db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    yield dm
    await dm.close()


def test_database_package_exports() -> None:
    """Verify core.database exports all expected public and internal interface symbols."""
    expected_symbols = [
        "Base",
        "DatabaseManager",
        "DeviceDatabasePool",
        "DeviceMetaRow",
        "DeviceRegistry",
        "DeviceState",
        "DNSQueryLog",
        "FieldMapping",
        "InterceptedRequest",
        "LLMContextBuffer",
        "LLMProfile",
        "MatchStats",
        "ModelRegistry",
        "RelayedConnection",
        "RequestPattern",
        "ResponseTemplate",
        "SessionCache",
        "_SQLITE_PRAGMAS",
        "_apply_sqlite_pragmas",
        "_collapse_dns_observations",
        "_db_manager",
        "_iso_utc",
        "create_configured_engine",
        "get_db_manager",
        "init_db_manager",
    ]
    for symbol in expected_symbols:
        assert hasattr(db_pkg, symbol), f"core.database missing symbol {symbol}"


def test_direct_submodule_imports() -> None:
    """Verify symbols can be imported directly from focused submodules."""
    assert engine_mod.create_configured_engine is create_configured_engine
    assert pool_mod.DeviceDatabasePool is DeviceDatabasePool
    assert manager_mod.DatabaseManager is DatabaseManager
    assert manager_mod.get_db_manager is get_db_manager
    assert manager_mod.init_db_manager is init_db_manager


@pytest.mark.asyncio
async def test_device_database_pool_standalone(tmp_path):
    """Verify DeviceDatabasePool can be used directly for per-device SQLite operations."""
    pool = DeviceDatabasePool(tmp_path / "devices")

    # Safe name generation
    assert pool._safe_db_name("device:123/abc") == "device_123_abc.db"
    assert pool._is_ip("192.168.1.100") is True
    assert pool._inside_device_dir(tmp_path / "devices" / "test.db") is True

    # Engine creation and schema creation
    engine = await pool.get_device_engine("dev-standalone-1")
    assert engine is not None
    assert "dev-standalone-1" in pool._device_engines

    # Sessionmaker creation
    async with pool.device_session("dev-standalone-1") as session:
        result = await session.execute(Base.metadata.tables["request_patterns"].select())
        assert result.all() == []

    # Close engine
    await pool.close_device_engine("dev-standalone-1")
    assert "dev-standalone-1" not in pool._device_engines

    await pool.close()


@pytest.mark.asyncio
async def test_database_manager_delegation(temp_db_manager):
    """Verify DatabaseManager delegates device operations to DeviceDatabasePool seamlessly."""
    dm = temp_db_manager

    # Property access backward compatibility
    assert dm._device_engines is dm.device_pool._device_engines
    assert dm._device_sessions is dm.device_pool._device_sessions
    assert dm._device_db_urls is dm.device_pool._device_db_urls
    assert dm._device_locks is dm.device_pool._device_locks

    # Creation of device through dm
    await dm.get_or_create_device("dev-delegation-1", "acme")
    assert "dev-delegation-1" in dm._device_engines

    # Listing databases
    dbs = await dm.list_databases()
    dev_ids = [d["device_id"] for d in dbs]
    assert "dev-delegation-1" in dev_ids

    # Storage ids
    storage_ids = await dm.list_device_ids_with_storage()
    assert "dev-delegation-1" in storage_ids
