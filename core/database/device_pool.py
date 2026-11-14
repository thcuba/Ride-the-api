"""Per-device SQLite connection pooling, engine lifecycle, and file management."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from core.database.engine import create_configured_engine
from core.migrations import SchemaMigrator
from core.models import Base

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class DeviceDatabasePool:
    """Manages the pool of per-device SQLite engines, sessions, and files."""

    DEVICE_DB_SCHEMES = frozenset({"sqlite+aiosqlite", "postgresql+asyncpg"})
    _SQLITE_PREFIX = "sqlite+aiosqlite:///"
    _SAFE_DB_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")

    def __init__(
        self,
        device_db_dir: Path,
        *,
        device_db_urls: dict[str, str] | None = None,
        echo: bool = False,
    ) -> None:
        self.device_db_dir = Path(device_db_dir)
        self.device_db_dir.mkdir(parents=True, exist_ok=True)
        self.echo = echo

        self._device_engines: dict[str, AsyncEngine] = {}
        self._device_sessions: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._device_db_urls = device_db_urls if device_db_urls is not None else {}
        self._engine_locks: dict[str, asyncio.Lock] = {}
        self._device_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def _safe_db_name(cls, device_id: str) -> str:
        """Sanitize a device ID into a safe database file name with .db extension."""
        cleaned = cls._SAFE_DB_NAME_RE.sub("_", device_id)
        if not cleaned.endswith(".db"):
            cleaned = f"{cleaned}.db"
        return cleaned

    @staticmethod
    def _is_ip(value: str) -> bool:
        """Fast check if value is a valid IPv4 or IPv6 address."""
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return False
        else:
            return True

    def _inside_device_dir(self, path: Path) -> bool:
        """Verify path is contained within the device database directory."""
        return path.resolve().is_relative_to(self.device_db_dir.resolve())

    def _default_device_db_url(self, device_id: str) -> str:
        """Return the default SQLite URL for a device."""
        db_path = self.device_db_dir / self._safe_db_name(device_id)
        return f"{self._SQLITE_PREFIX}{db_path}"

    def checked_device_db_url(self, database_url: str) -> str:
        """Validate database_url for assignment to a device, else raise ValueError."""
        scheme, sep, _rest = database_url.partition("://")
        if not sep or scheme.lower() not in self.DEVICE_DB_SCHEMES:
            allowed = ", ".join(sorted(self.DEVICE_DB_SCHEMES))
            raise ValueError(f"database_url must use one of: {allowed}")
        if scheme.lower() == "sqlite+aiosqlite":
            raw_path = database_url[len(self._SQLITE_PREFIX) :].split("?", 1)[0]
            if (
                not raw_path
                or raw_path == ":memory:"
                or not self._inside_device_dir(Path(raw_path))
            ):
                raise ValueError(
                    "a SQLite device database must be a file inside the device database directory"
                )
        return database_url

    def resolve_device_db_path(
        self, device_id: str, custom_url: str | None = None
    ) -> Path | None:
        """Return the on-disk SQLite file backing a device, or None."""
        db_url = self._device_db_urls.get(device_id) or custom_url
        if not db_url:
            return self.device_db_dir / self._safe_db_name(device_id)
        if not db_url.startswith(self._SQLITE_PREFIX):
            return None
        return Path(db_url[len(self._SQLITE_PREFIX) :])

    def _move_db_file(self, old_path: Path, new_path: Path) -> None:
        """Move a device DB file with its SQLite sidecars (-wal, -shm)."""
        if not old_path.exists():
            return
        if not self._inside_device_dir(old_path):
            raise ValueError("the database file is outside the device database directory")
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{old_path}{suffix}")
            if sidecar.exists():
                sidecar.rename(Path(f"{new_path}{suffix}"))

    def get_device_lock(self, device_id: str) -> asyncio.Lock:
        """Get or create per-device serialization lock."""
        return self._device_locks.setdefault(device_id, asyncio.Lock())

    async def get_device_engine(
        self, device_id: str, *, custom_url: str | None = None
    ) -> AsyncEngine:
        """Get or create a device-specific database engine."""
        if device_id in self._device_engines:
            return self._device_engines[device_id]

        lock = self._engine_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            if device_id in self._device_engines:
                return self._device_engines[device_id]

            if device_id in self._device_db_urls:
                db_url = self._device_db_urls[device_id]
            elif custom_url:
                db_url = custom_url
                self._device_db_urls[device_id] = custom_url
            else:
                db_path = self.device_db_dir / self._safe_db_name(device_id)
                db_url = f"{self._SQLITE_PREFIX}{db_path}"

            engine = create_configured_engine(db_url, echo=self.echo)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await SchemaMigrator(engine).run()
            self._device_engines[device_id] = engine
            self._device_sessions[device_id] = async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            logger.info("Device database '%s' initialized at %s", device_id, db_url)
            return engine

    async def get_device_session(
        self, device_id: str, *, custom_url: str | None = None
    ) -> AsyncSession:
        """Get an AsyncSession for a device (caller must manage lifecycle)."""
        await self.get_device_engine(device_id, custom_url=custom_url)
        return self._device_sessions[device_id]()

    @contextlib.asynccontextmanager
    async def device_session(
        self, device_id: str, *, custom_url: str | None = None
    ) -> AsyncGenerator[AsyncSession, None]:
        """Provide an AsyncSession context for a specific device's database."""
        session = await self.get_device_session(device_id, custom_url=custom_url)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def close_device_engine(self, device_id: str) -> None:
        """Dispose and remove engine and sessionmaker for a device."""
        engine = self._device_engines.pop(device_id, None)
        if engine is not None:
            await engine.dispose()
        self._device_sessions.pop(device_id, None)

    def delete_storage_files(self, db_path: Path) -> None:
        """Delete database file and SQLite WAL/SHM sidecars."""
        if db_path.exists():
            db_path.unlink()
            for sidecar in ("-wal", "-shm"):
                Path(f"{db_path}{sidecar}").unlink(missing_ok=True)

    def list_device_ids_with_storage(self) -> list[str]:
        """Device ids having a SQLite database file in the device DB directory."""
        if not self.device_db_dir.exists():
            return []
        ids = []
        for file in self.device_db_dir.glob("*.db"):
            stem = file.stem
            if stem.endswith(".ride-pattern"):
                continue
            ids.append(stem)
        return sorted(ids)

    def list_databases(self, devices: list[Any]) -> list[dict]:
        """Format database inventory for registered devices."""
        databases: list[dict] = []
        for device in devices:
            databases.append(
                {
                    "device_id": device.device_id,
                    "database_url": (
                        self._device_db_urls.get(device.device_id)
                        or device.database_url
                        or self._default_device_db_url(device.device_id)
                    ),
                    "is_active": device.device_id in self._device_engines,
                }
            )
        return databases

    async def close(self) -> None:
        """Dispose all open device engines."""
        for _device_id, engine in list(self._device_engines.items()):
            await engine.dispose()
        self._device_engines.clear()
        self._device_sessions.clear()


__all__ = ["DeviceDatabasePool"]
