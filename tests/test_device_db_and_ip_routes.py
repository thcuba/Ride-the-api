"""Device database assignment is confined (S-16); IP binding moves the address (C-23)."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from core import server as server_mod
from core.database import DatabaseManager, DeviceRegistry

OK, BAD = 200, 400


@pytest_asyncio.fixture
async def dm(tmp_path):
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await manager.initialize()
    await manager.get_or_create_device("plug", "acme", "plug")
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def client(dm, monkeypatch):
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _assign(client: httpx.AsyncClient, **body) -> httpx.Response:
    return await client.post("/api/devices/plug/database", json=body)


@pytest.mark.asyncio
async def test_database_urls_outside_the_device_dir_are_refused(client, dm, tmp_path):
    inside = dm.device_db_dir / "plug-store.db"
    refused = [
        f"sqlite+aiosqlite:///{tmp_path / 'elsewhere.db'}",
        f"sqlite+aiosqlite:///{dm.device_db_dir}/../escape.db",
        "sqlite+aiosqlite:///:memory:",
        "postgresql://localhost/plug",  # a sync driver cannot back an async engine
        "mysql+aiomysql://localhost/plug",
        "file:///etc/passwd",
    ]
    for url in refused:
        response = await _assign(client, database_url=url)
        assert response.status_code == BAD, url

    assert (await _assign(client, database_url=f"sqlite+aiosqlite:///{inside}")).status_code == OK
    assert (await _assign(client, database_url="postgresql+asyncpg://db/plug")).status_code == OK
    named = await _assign(client, database_name="plug-named")
    assert named.status_code == OK
    assert named.json()["database_url"].endswith("plug-named.db")


@pytest.mark.asyncio
async def test_rename_never_moves_a_file_outside_the_device_dir(dm, tmp_path):
    outside = tmp_path / "precious.db"
    outside.write_bytes(b"not ours")
    async with dm.core_session() as session:
        device = (
            await session.execute(select(DeviceRegistry).where(DeviceRegistry.device_id == "plug"))
        ).scalar_one()
        device.database_url = f"sqlite+aiosqlite:///{outside}"  # assigned before validation
        await session.commit()
    dm.invalidate_device_cache("plug")

    with pytest.raises(ValueError, match="outside"):
        await dm.rename_device_database("plug", "renamed")
    assert outside.read_bytes() == b"not ours"


@pytest.mark.asyncio
async def test_binding_an_ip_takes_it_from_its_previous_holder(client, dm):
    await dm.get_or_create_device("ip-192-168-1-50", "unknown", "unknown")
    async with dm.core_session() as session:
        auto = (
            await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "ip-192-168-1-50")
            )
        ).scalar_one()
        auto.ip_addresses = ["192.168.1.50"]
        await session.commit()
    # The real device is registered after its traffic auto-registered an ip-* one.
    await dm.get_or_create_device("thermostat", "acme", "thermostat")
    dm.invalidate_ip_lookup_cache()
    assert await dm.resolve_device_id("192.168.1.50") == "ip-192-168-1-50"

    response = await client.post("/api/devices/thermostat/ip", json={"ip_address": "192.168.1.50"})
    assert response.status_code == OK
    assert response.json()["ip_addresses"] == ["192.168.1.50"]
    assert await dm.resolve_device_id("192.168.1.50") == "thermostat"
    async with dm.core_session() as session:
        auto = (
            await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == "ip-192-168-1-50")
            )
        ).scalar_one()
    assert auto.ip_addresses == []
