"""
Core Database Architecture - Device-Specific Protocol Databases
Core DB + Per-Device DB (SQLite default, PostgreSQL optional)
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Index,
    Integer,
    String,
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from core.config import get_config
from core.migrations import SchemaMigrator

logger = logging.getLogger(__name__)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BASE CLASSES
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


# Sqlite durability settings applied to every engine connection. WAL journaling
# plus synchronous=NORMAL keeps reads concurrent and commits crash-safe without
# the write-lock stalls of the default rollback journal. busy_timeout avoids
# "database is locked" under concurrent writers, and foreign_keys enforce
# referential integrity the default SQLite doesn't.
_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Apply durability PRAGMAs to a SQLite connection (no-op otherwise)."""
    try:
        cursor = dbapi_connection.cursor()
        for pragma in _SQLITE_PRAGMAS:
            cursor.execute(pragma)
        cursor.close()
    except Exception:  # pragma: no cover - defensive for non-sqlite drivers
        return


def create_configured_engine(db_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine with durability PRAGMAs for SQLite backends.

    Uses NullPool (the de-facto setting for aiosqlite here) so that every
    connection re-runs the PRAGMAs via the connect listener. PostgreSQL accepts
    no SQLite PRAGMAs; the listener returns without touching them.
    """
    engine = create_async_engine(db_url, echo=echo, poolclass=NullPool)
    if db_url.startswith("sqlite"):
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CORE DATABASE MODELS (shared across all devices)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


class DeviceRegistry(Base):
    """Core device registry - maps device_id to its protocol DB."""

    __tablename__ = "device_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False
    )  # protocol identifier
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # IP addresses associated with this device (for routing by IP)
    ip_addresses: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Database assignment (optional â€” override default per-device db)
    database_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Learning / Production / Hybrid mode
    mode: Mapped[str] = mapped_column(
        String(16), default="learning", nullable=False
    )  # learning | production | hybrid

    # Match threshold for production mode
    match_threshold: Mapped[float] = mapped_column(Float, default=0.85, nullable=False)

    # Auto-switch to production when match rate >= 99%
    auto_switch_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)

    # LLM configuration (per-device override)
    llm_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    llm_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_profile_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Extra attributes for TLS passthrough, pinning bypass, etc.
    extra_attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Context buffer configuration
    context_buffer_size: Mapped[int] = mapped_column(
        Integer, default=524288, nullable=False
    )  # 512KB default

    # User-defined context notes for LLM analysis (injected as {context_notes})
    llm_context_notes: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="online", nullable=False)
    last_seen: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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

    class LLMProfile(Base):
        """User-saved LLM decipher profiles/templates."""

        __tablename__ = "llm_profiles"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
        description: Mapped[str | None] = mapped_column(String(512), nullable=True)
        base_url: Mapped[str] = mapped_column(String(512), nullable=False)
        api_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)
        model_id: Mapped[str] = mapped_column(String(128), nullable=False)
        prompt_template: Mapped[str] = mapped_column(String(16384), nullable=False)
        enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
        is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
        created_at: Mapped[DateTime] = mapped_column(
            DateTime(timezone=True), server_default=func.now()
        )
        updated_at: Mapped[DateTime] = mapped_column(
            DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
        )

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # DEVICE-SPECIFIC DATABASE MODELS (each device gets its own DB with these tables)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


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
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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
    captured_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
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
    protocol: Mapped[str] = mapped_column(String(32), default="", nullable=False)

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

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InterceptedRequest(Base):
    """Raw intercepted request/response pair for audit / training data."""

    __tablename__ = "intercepted_requests"
    __table_args__ = (Index("ix_intercepted_device_ts", "device_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    captured_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

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


class DeviceState(Base):
    """Persisted snapshot of a device's simulated state (single row per device).
    
    Stores the last known ``state_variables`` so that ``{state.xxx}`` values and
    virtual-sensor baselines survive a restart. Written whenever a response's
    field mappings mutate the state store.
    """
    
    __tablename__ = "device_state"
    
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[DateTime] = mapped_column(
    DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DATABASE MANAGER
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


class DatabaseManager:
    """Manages core DB + per-device DBs."""

    def __init__(
        self,
        core_db_url: str,
        device_db_dir: Path,
        device_db_urls: dict[str, str] | None = None,
        echo: bool = False,
    ) -> None:
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
        self._core_engine = create_configured_engine(self.core_db_url, echo=self.echo)
        self._core_session_factory = async_sessionmaker(
            self._core_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self._core_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await SchemaMigrator(self._core_engine).run()
        logger.info("Core database initialized")

    @staticmethod
    def _is_ip(value: str) -> bool:
        """Check if a string looks like an IPv4 address."""
        parts = value.split(".")
        if len(parts) != 4:  # noqa: PLR2004
            return False
        try:
            return all(0 <= int(p) <= 255 for p in parts)  # noqa: PLR2004
        except ValueError:
            return False

    async def _get_device_db_url(self, device_id: str) -> str | None:
        """Look up a device's custom database URL from the registry."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if device and device.database_url:
                return device.database_url
            return None

    async def resolve_device_id(self, ip_address: str) -> str | None:
        """Resolve device_id from an IP address (reverse lookup).

        Matches exact IPv4 membership in the per-device ``ip_addresses`` list.
        ``JSON.contains`` is a substring test (``LIKE '%ip%'``) â€” a partial
        IPv4 like ``192.168.1.1`` matched a device storing ``192.168.1.100`` and
        routed its traffic to the wrong device DB. Load candidate rows and test
        exact membership in Python for portable SQLite/Postgres behaviour.
        """
        async with await self.get_core_session() as session:
            result = await session.execute(select(DeviceRegistry))
            for device in result.scalars().all():
                if ip_address in (device.ip_addresses or []):
                    return device.device_id
            return None

    async def assign_device_database(
        self,
        device_id: str,
        database_url: str | None = None,
        database_name: str | None = None,
    ) -> bool:
        """Assign a specific database URL or name to a device."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            if database_url:
                device.database_url = database_url
            if database_name:
                device.database_name = database_name
            await session.commit()
            # Re-initialize engine with new URL
            if device_id in self._device_engines:
                old = self._device_engines.pop(device_id)
                await old.dispose()
            if device_id in self._device_sessions:
                del self._device_sessions[device_id]
            if database_url:
                self._device_db_urls[device_id] = database_url
            logger.info(f"Device {device_id} database assigned: {database_url or database_name}")
            return True

    async def list_databases(self) -> list[dict]:
        """List all device databases with their assignments."""
        databases: list[dict] = []
        for device_id, engine in self._device_engines.items():
            db_url = self._device_db_urls.get(device_id, "")
            if not db_url:
                db_path = self.device_db_dir / f"{device_id}.db"
                db_url = f"sqlite+aiosqlite:///{db_path}"
            databases.append(
                {
                    "device_id": device_id,
                    "database_url": db_url,
                    "is_active": True,
                }
            )
        return databases

    async def get_device_engine(self, device_id: str) -> AsyncEngine:
        """Get or create a device-specific database engine.
        Checks device_registry for a custom database_url override first.
        Also supports resolving device_id from an IP address."""
        # IP resolution: if device_id looks like an IP, try reverse lookup
        if device_id not in self._device_engines and self._is_ip(device_id):
            resolved = await self.resolve_device_id(device_id)
            if resolved:
                device_id = resolved

        if device_id in self._device_engines:
            return self._device_engines[device_id]
        if device_id in self._device_db_urls:
            db_url = self._device_db_urls[device_id]
        else:
            # Check registry for custom database URL
            custom_url = await self._get_device_db_url(device_id)
            if custom_url:
                db_url = custom_url
                self._device_db_urls[device_id] = custom_url
            else:
                db_path = self.device_db_dir / f"{device_id}.db"
                db_url = f"sqlite+aiosqlite:///{db_path}"
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

    async def get_or_create_device(
        self, device_id: str, vendor: str, device_type: str = "unknown", name: str = ""
    ) -> None:
        """Ensure a device exists in the registry and create its DB."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                # Inherit global learning defaults into the per-device config
                try:
                    learning = get_config().learning
                    device_config = {
                        "production_no_fallback": learning.production_no_fallback,
                    }
                except Exception:
                    device_config = {}
                device = DeviceRegistry(
                    device_id=device_id,
                    vendor=vendor,
                    device_type=device_type,
                    name=name or device_id,
                    mode="learning",
                    config=device_config,
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
                    "device_id": d.device_id,
                    "vendor": d.vendor,
                    "device_type": d.device_type,
                    "name": d.name,
                    "mode": d.mode,
                    "match_threshold": d.match_threshold,
                    "status": d.status,
                    "last_seen": d.last_seen.isoformat() if d.last_seen else None,
                    "context_buffer_size": d.context_buffer_size,
                    "llm_model_id": d.llm_model_id,
                    "llm_base_url": d.llm_base_url,
                    "llm_profile_name": d.llm_profile_name,
                    "llm_context_notes": d.llm_context_notes,
                    "auto_switch_enabled": d.auto_switch_enabled,
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

    async def update_device_llm_config(
        self,
        device_id: str,
        base_url: str | None = None,
        model_id: str | None = None,
        profile_name: str | None = None,
    ) -> bool:
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

    async def update_device_auto_switch(self, device_id: str, enabled: bool) -> bool:
        """Enable or disable auto-switch to production for a device."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            device.auto_switch_enabled = enabled
            await session.commit()
            logger.info(f"Device {device_id} auto-switch {'enabled' if enabled else 'disabled'}")
            return True

    # â”€â”€ Context notes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def update_device_context_notes(self, device_id: str, notes: str) -> bool:
        """Update custom context notes for a device (injected as {context_notes})."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            device.llm_context_notes = notes
            await session.commit()
            return True

    async def get_device_context_notes(self, device_id: str) -> str | None:
        """Get custom context notes for a device."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            return device.llm_context_notes if device else None

    # â”€â”€ LLM Profile CRUD â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def list_llm_profiles(self) -> list[dict]:
        """List all user-saved LLM profiles."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(ModelRegistry.LLMProfile).order_by(ModelRegistry.LLMProfile.name)
            )
            return [
                {
                    "name": p.name,
                    "description": p.description,
                    "base_url": p.base_url,
                    "model_id": p.model_id,
                    "prompt_template": p.prompt_template,
                    "enabled": p.enabled,
                    "is_default": p.is_default,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                }
                for p in result.scalars().all()
            ]

    async def get_llm_profile(self, name: str) -> dict | None:
        """Get a single LLM profile by name."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(ModelRegistry.LLMProfile).where(ModelRegistry.LLMProfile.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                return None
            return {
                "name": p.name,
                "description": p.description,
                "base_url": p.base_url,
                "model_id": p.model_id,
                "api_key": "***" if p.api_key else "",
                "prompt_template": p.prompt_template,
                "enabled": p.enabled,
                "is_default": p.is_default,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }

    async def create_llm_profile(self, data: dict) -> bool:
        """Create a new LLM profile."""
        async with await self.get_core_session() as session:
            existing = await session.execute(
                select(ModelRegistry.LLMProfile).where(
                    ModelRegistry.LLMProfile.name == data["name"]
                )
            )
            if existing.scalar_one_or_none():
                return False
            profile = ModelRegistry.LLMProfile(
                name=data["name"],
                description=data.get("description"),
                base_url=data.get("base_url", ""),
                api_key=data.get("api_key", ""),
                model_id=data.get("model_id", ""),
                prompt_template=data.get("prompt_template", ""),
                enabled=data.get("enabled", True),
                is_default=data.get("is_default", False),
            )
            session.add(profile)
            await session.commit()
            return True

    async def update_llm_profile(self, name: str, data: dict) -> bool:
        """Update an existing LLM profile."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(ModelRegistry.LLMProfile).where(ModelRegistry.LLMProfile.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                return False
            if "description" in data:
                p.description = data["description"]
            if "base_url" in data:
                p.base_url = data["base_url"]
            if "api_key" in data:
                p.api_key = data["api_key"]
            if "model_id" in data:
                p.model_id = data["model_id"]
            if "prompt_template" in data:
                p.prompt_template = data["prompt_template"]
            if "enabled" in data:
                p.enabled = data["enabled"]
            if "is_default" in data:
                p.is_default = data["is_default"]
            await session.commit()
            return True

    async def delete_llm_profile(self, name: str) -> bool:
        """Delete an LLM profile."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(ModelRegistry.LLMProfile).where(ModelRegistry.LLMProfile.name == name)
            )
            p = result.scalar_one_or_none()
            if not p:
                return False
            await session.delete(p)
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
    if _db_manager is None:
        raise RuntimeError("DatabaseManager not initialized. Call init_db_manager first.")
    return _db_manager


def init_db_manager(
    core_db_url: str,
    device_db_dir: Path,
    device_db_urls: dict[str, str] | None = None,
    echo: bool = False,
) -> DatabaseManager:
    """Initialize the global database manager."""
    global _db_manager  # noqa: PLW0603
    _db_manager = DatabaseManager(core_db_url, device_db_dir, device_db_urls, echo)
    return _db_manager
