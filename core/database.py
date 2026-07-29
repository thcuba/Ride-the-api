"""
Core Database Architecture - Device-Specific Protocol Databases
Core DB + Per-Device DB (SQLite default, PostgreSQL optional)
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# BASE CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class Base(DeclarativeBase):
    """Base class for all models."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# CORE DATABASE MODELS (shared across all devices)
# ═══════════════════════════════════════════════════════════════════════════════

class DeviceRegistry(Base):
    """Core device registry - maps device_id to its protocol DB."""
    __tablename__ = "device_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(32), index=True, nullable=False)  # protocol identifier
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Learning / Production mode
    mode: Mapped[str] = mapped_column(String(16), default="learning", nullable=False)  # learning | production

    # Match threshold for production mode
    match_threshold: Mapped[float] = mapped_column(Float, default=0.85, nullable=False)

    # LLM configuration (per-device override)
    llm_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    llm_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_profile_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Context buffer configuration
    context_buffer_size: Mapped[int] = mapped_column(Integer, default=524288, nullable=False)  # 512KB default

    status: Mapped[str] = mapped_column(String(32), default="online", nullable=False)
    last_seen: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ModelRegistry(Base):
    """Core model registry - tracks models per device."""
    __tablename__ = "model_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    model_path: Mapped[str] = mapped_column(String(512), nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE-SPECIFIC DATABASE MODELS (each device gets its own DB with these tables)
# ═══════════════════════════════════════════════════════════════════════════════

class RequestPattern(Base):
    """Learned request pattern for matching incoming requests."""
    __tablename__ = "request_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # Request signature
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path_pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)

    # Header requirements (keys that must be present)
    required_headers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Body schema (JSONSchema-like)
    body_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Query params typically present
    query_param_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Intent (deciphered by LLM)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)

    # Confidence in this pattern
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)

    # Usage statistics
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_matched: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ResponseTemplate(Base):
    """Learned response template for local response building."""
    __tablename__ = "response_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # Links to the request pattern this response matches
    pattern_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    # Response template
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    headers_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Field mappings: which request fields map to which response fields
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Variables expected in the body template
    expected_variables: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Confidence
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)

    # Usage
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FieldMapping(Base):
    """LLM-decoded field mapping between request and response."""
    __tablename__ = "field_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # Source field in request
    request_field: Mapped[str] = mapped_column(String(128), nullable=False)
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Destination field in response
    response_field: Mapped[str] = mapped_column(String(128), nullable=False)
    response_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Transformation (if any)
    transform: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Enum values if type is enum
    enum_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Context / intent this mapping belongs to
    intent: Mapped[str] = mapped_column(String(64), nullable=False)

    # Confidence from LLM
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMContextBuffer(Base):
    """Sliding-window context buffer for LLM batch analysis."""
    __tablename__ = "llm_context_buffer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    # The correlated request/response pair data (serialized)
    correlated_pair: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Estimated size in bytes
    estimated_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Sequence number (for ordering)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # Whether this entry has been flushed to LLM
    flushed: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Timestamps
    captured_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    flushed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionCache(Base):
    """Temporary correlation cache - cleared after each learning cycle flush."""
    __tablename__ = "session_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    # Correlation key for matching
    correlation_key: Mapped[str] = mapped_column(String(256), nullable=False)

    # Pending request data
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Response data (filled when correlated)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Correlation status
    correlated: Mapped[bool] = mapped_column(default=False, nullable=False)
    correlated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Whether this entry was already sent to context buffer
    in_buffer: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MatchStats(Base):
    """Real-time match statistics per device."""
    __tablename__ = "match_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    # Running counters
    total_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    local_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cloud_misses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Current match rate
    match_rate_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Rolling window for recent requests (last 1000)
    recent_results: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Learning mode specific
    patterns_learned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    templates_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buffer_flushes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_buffer_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_flush_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class InterceptedRequest(Base):
    """Raw intercepted request/response pair for audit / training data."""
    __tablename__ = "intercepted_requests"
    __table_args__ = (
        Index("ix_intercepted_device_ts", "device_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    captured_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    processed: Mapped[bool] = mapped_column(default=False, nullable=False)
    processed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class DatabaseManager:
    """Manages core DB + per-device DBs."""

    def __init__(
        self,
        core_db_url: str,
        device_db_dir: Path,
        device_db_urls: dict[str, str] | None = None,
        echo: bool = False,
    ):
        self.core_db_url = core_db_url
        self.device_db_dir = Path(device_db_dir)
        self.device_db_dir.mkdir(parents=True, exist_ok=True)
        self.echo = echo

        self._core_engine: AsyncEngine | None = None
        self._core_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._device_engines: dict[str, AsyncEngine] = {}
        self._device_sessions: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._device_db_urls = device_db_urls or {}

    async def initialize(self) -> None:
        """Initialize all databases."""
        self._core_engine = create_async_engine(
            self.core_db_url, echo=self.echo, poolclass=NullPool,
        )
        self._core_session_factory = async_sessionmaker(
            self._core_engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with self._core_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Core database initialized")

    async def get_device_engine(self, device_id: str) -> AsyncEngine:
        """Get or create a device-specific database engine."""
        if device_id in self._device_engines:
            return self._device_engines[device_id]
        if device_id in self._device_db_urls:
            db_url = self._device_db_urls[device_id]
        else:
            db_path = self.device_db_dir / f"{device_id}.db"
            db_url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(db_url, echo=self.echo, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._device_engines[device_id] = engine
        self._device_sessions[device_id] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )
        logger.info(f"Device database '{device_id}' initialized at {db_url}")
        return engine

    async def get_device_session(self, device_id: str) -> AsyncSession:
        """Get an async session for a device database."""
        await self.get_device_engine(device_id)
        return self._device_sessions[device_id]()

    async def get_core_session(self) -> AsyncSession:
        """Get an async session for the core database."""
        if not self._core_session_factory:
            raise RuntimeError("Core database not initialized")
        return self._core_session_factory()

    async def get_or_create_device(self, device_id: str, vendor: str,
                                    device_type: str = "unknown", name: str = "") -> None:
        """Ensure a device exists in the registry and create its DB."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                device = DeviceRegistry(
                    device_id=device_id, vendor=vendor,
                    device_type=device_type, name=name or device_id,
                    mode="learning",
                )
                session.add(device)
                await session.commit()
                logger.info(f"Registered new device: {device_id} ({vendor})")
        await self.get_device_engine(device_id)

    @contextlib.asynccontextmanager
    async def core_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Context manager for core DB session."""
        session = await self.get_core_session()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @contextlib.asynccontextmanager
    async def device_session(self, device_id: str) -> AsyncGenerator[AsyncSession, None]:
        """Context manager for device DB session."""
        session = await self.get_device_session(device_id)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def list_devices(self) -> list[dict]:
        """List all registered devices."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).order_by(DeviceRegistry.created_at.desc())
            )
            return [
                {
                    "device_id": d.device_id, "vendor": d.vendor,
                    "device_type": d.device_type, "name": d.name,
                    "mode": d.mode, "match_threshold": d.match_threshold,
                    "status": d.status,
                    "last_seen": d.last_seen.isoformat() if d.last_seen else None,
                    "context_buffer_size": d.context_buffer_size,
                    "llm_model_id": d.llm_model_id,
                    "llm_base_url": d.llm_base_url,
                }
                for d in result.scalars().all()
            ]

    async def update_device_mode(self, device_id: str, mode: str) -> bool:
        """Switch device between learning and production mode."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            device.mode = mode
            await session.commit()
            logger.info(f"Device {device_id} switched to {mode} mode")
            return True

    async def update_device_llm_config(self, device_id: str, base_url: str | None = None,
                                        model_id: str | None = None,
                                        profile_name: str | None = None) -> bool:
        """Update LLM configuration for a device."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            if base_url is not None:
                device.llm_base_url = base_url
            if model_id is not None:
                device.llm_model_id = model_id
            if profile_name is not None:
                device.llm_profile_name = profile_name
            await session.commit()
            return True

    async def close(self) -> None:
        """Close all database connections."""
        if self._core_engine:
            await self._core_engine.dispose()
        for engine in self._device_engines.values():
            await engine.dispose()
        logger.info("All database connections closed")


# Global instance
_db_manager: DatabaseManager | None = None


def get_db_manager() -> DatabaseManager:
    """Get or create global database manager instance."""
    global _db_manager
    if _db_manager is None:
        raise RuntimeError("DatabaseManager not initialized. Call init_db_manager first.")
    return _db_manager


def init_db_manager(core_db_url: str, device_db_dir: Path,
                    device_db_urls: dict[str, str] | None = None,
                    echo: bool = False) -> DatabaseManager:
    """Initialize the global database manager."""
    global _db_manager
    _db_manager = DatabaseManager(core_db_url, device_db_dir, device_db_urls, echo)
    return _db_manager