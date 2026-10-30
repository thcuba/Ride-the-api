"""
Core Database Architecture - Device-Specific Protocol Databases
Core DB + Per-Device DB (SQLite default, PostgreSQL optional)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from cachetools import TTLCache
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    delete,
    event,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from core.atomic_io import sanitize_filename_component
from core.config import get_config
from core.migrations import SchemaMigrator
from core.pattern_db.schemas import DeviceMeta

logger = logging.getLogger(__name__)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501
# BASE CLASSES
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501


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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501
# CORE DATABASE MODELS (shared across all devices)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501


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

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501


class LLMProfile(Base):
    """User-saved LLM decipher profiles/templates.

    Declared at module level (not nested inside ``ModelRegistry``) so schema
    introspection and migration tooling discover it via ``Base.metadata``.
    """

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
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # DEVICE-SPECIFIC DATABASE MODELS (each device gets its own DB with these tables)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501


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


class RelayedConnection(Base):
    """A connection forwarded to the vendor cloud without terminating TLS.

    Some devices pin their vendor's certificate and will never accept one we generate, so their
    traffic is relayed verbatim and nothing of the payload is readable. The contact itself is still
    worth keeping: which device, which host it asked for, how much was exchanged in each direction
    and how long it lasted. That is the only trace such a device leaves, and the raw material from
    which its behaviour can be understood over time.
    """

    __tablename__ = "relayed_connections"
    __table_args__ = (Index("ix_relayed_device_ts", "device_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    client_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    server_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    destination_port: Mapped[int] = mapped_column(Integer, nullable=False)
    upstream_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_to_cloud: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_to_device: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # relayed | no_server_name | unresolvable | unreachable
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


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


class DeviceMetaRow(Base):
    """Per-device header persisted in the device DB (single row per device).

    Stores the structured, machine-readable header written at the first LLM
    flush (see :class:`core.pattern_db.schemas.DeviceMeta`): the protocol(s)
    the device speaks and the ingress ``connection_mode`` that the server reads
    to select the right handler. It is stable once set.
    """

    __tablename__ = "device_meta"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DNSQueryLog(Base):
    """One (device, hostname) pair seen on the DNS interception port.

    The DNS server is the first place a device reveals what it talks to, whether
    or not the name is being intercepted: what a client asks for, how often and
    when is what lets an operator decide what to intercept next instead of
    guessing domain names. One row per (client_ip, hostname, qtype) with a hit
    counter, so the table grows with the number of distinct pairs rather than
    with the number of queries; the oldest rows are pruned past a cap.
    """

    __tablename__ = "dns_query_log"
    __table_args__ = (
        UniqueConstraint("client_ip", "hostname", "qtype", name="uq_dns_query_pair"),
        Index("ix_dns_query_last_seen", "last_seen"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_ip: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False, default="A")
    # intercepted | excluded | forwarded -- what the proxy did with this name.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="forwarded")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501
def _iso_utc(value: datetime | None) -> str | None:
    """Render a stored timestamp as ISO-8601 UTC.

    SQLite hands the rows back naive (it has no timezone type), so an unqualified
    ``isoformat()`` would leave the caller guessing -- and comparing a naive string
    with the aware one of a not-yet-flushed counter is not the same comparison.
    """
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _collapse_dns_observations(observations: list[dict]) -> list[dict]:
    """Merge observations sharing (client_ip, hostname, qtype) into one row each.

    A batch must not carry the same unique key twice: SQLite refuses a single
    ``ON CONFLICT DO UPDATE`` statement that would touch one row two times, so
    collapsing here keeps a duplicated pair a hit counter, not a failed flush.
    """
    merged: dict[tuple, dict] = {}
    for obs in observations:
        key = (obs["client_ip"], obs["hostname"], obs["qtype"])
        current = merged.get(key)
        if current is None:
            merged[key] = dict(obs)
            continue
        current["hit_count"] = int(current.get("hit_count") or 1) + int(obs.get("hit_count") or 1)
        current["first_seen"] = min(current["first_seen"], obs["first_seen"])
        current["last_seen"] = max(current["last_seen"], obs["last_seen"])
        current["status"] = obs["status"]
    return list(merged.values())


# DATABASE MANAGER
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•  # noqa: E501


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
        self._engine_locks: dict[str, asyncio.Lock] = {}
        # F-12: per-device locks serialize registry/device-DB creation.
        self._device_locks: dict[str, asyncio.Lock] = {}
        self._ip_lookup_cache: dict[str, str] | None = None

        # Bounded in-memory caches for device connection mode and device meta header (5-min TTL)
        self._device_connection_cache: TTLCache[str, str] = TTLCache(maxsize=2048, ttl=300)
        self._device_meta_cache: TTLCache[str, dict | None] = TTLCache(maxsize=2048, ttl=300)
        self._device_cache: TTLCache[str, DeviceRegistry | None] = TTLCache(maxsize=2048, ttl=300)

    def invalidate_device_cache(self, device_id: str | None = None) -> None:
        """Invalidate in-memory caches for a device or all devices."""
        if device_id is None:
            self._device_connection_cache.clear()
            self._device_meta_cache.clear()
            self._device_cache.clear()
        else:
            self._device_connection_cache.pop(device_id, None)
            self._device_meta_cache.pop(device_id, None)
            self._device_cache.pop(device_id, None)

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
        """Check if a string looks like an IP address (IPv4 or IPv6).

        Performance optimization: short-circuit non-IP strings that do not contain
        '.' or ':' before calling ipaddress.ip_address to avoid ValueError exception
        overhead on non-IP device IDs (~3.27x speedup).
        """
        if not value or ("." not in value and ":" not in value):
            return False
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return False
        else:
            return True

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

    def invalidate_ip_lookup_cache(self) -> None:
        """Drop the cached ip -> device_id reverse index.

        Call after any persistent change to a device's ``ip_addresses`` so the
        next reverse lookup re-reads the registry instead of returning stale
        device routes.
        """
        self._ip_lookup_cache = None

    async def count_relayed_connections(self) -> dict[str, int]:
        """Count relayed connections per device_id, in a single grouped query.

        Used by the device list. Fetching the count per device would be one query each; a grouped
        count keeps it at one regardless of how many devices exist.
        """
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(RelayedConnection.device_id, func.count(RelayedConnection.id)).group_by(
                    RelayedConnection.device_id
                )
            )
            return {device_id: total for device_id, total in result.all()}

    async def resolve_device_id(self, ip_address: str) -> str | None:
        """Resolve device_id from an IP address (reverse lookup).

        Matches exact IPv4 membership in the per-device ``ip_addresses`` list.
        ``JSON.contains`` is a substring test (``LIKE '%ip%'``) — a partial
        IPv4 like ``192.168.1.1`` matched a device storing ``192.168.1.100`` and
        routed its traffic to the wrong device DB. Load candidate rows and test
        exact membership in Python for portable SQLite/Postgres behaviour.

        The full registry is read once and cached keyed by IP; the cache is
        cleared via :meth:`invalidate_ip_lookup_cache` whenever a device's IP
        list changes (registration routes, bulk import, etc).
        """
        if self._ip_lookup_cache is None:
            async with await self.get_core_session() as session:
                result = await session.execute(select(DeviceRegistry))
                index: dict[str, str] = {}
                for device in result.scalars().all():
                    for ip in device.ip_addresses or []:
                        index.setdefault(ip, device.device_id)
                self._ip_lookup_cache = index
        return self._ip_lookup_cache.get(ip_address)

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
            self.invalidate_device_cache(device_id)
            logger.info(f"Device {device_id} database assigned: {database_url or database_name}")
            return True

    async def _resolve_device_db_path(self, device_id: str) -> Path | None:
        """Return the on-disk SQLite file backing a device, or ``None``.

        Resolution order matches :meth:`get_device_engine`: the runtime URL
        override, then the registry's ``database_url``, then the default file
        derived from the device id. Only local ``sqlite`` URLs map to a file;
        anything else (PostgreSQL, an in-memory database) has no path to move.
        """
        db_url = self._device_db_urls.get(device_id)
        if not db_url:
            db_url = await self._get_device_db_url(device_id)
        if not db_url:
            return self.device_db_dir / self._safe_db_name(device_id)
        prefix = "sqlite+aiosqlite:///"
        if not db_url.startswith(prefix):
            return None
        return Path(db_url[len(prefix) :])

    async def rename_device_database(
        self,
        device_id: str,
        new_name: str,
        *,
        move_file: bool = True,
    ) -> str | None:
        """Rename a device's database file and repoint the device at it.

        The file name follows the device name, so a device that is named after
        the fact ("living room purifier") stops being stored under a bare
        address. ``new_name`` is sanitized into a filename segment; the ``.db``
        suffix is added when missing.

        ``move_file`` renames the file on disk. Pass ``False`` to only record
        the new name and URL, which is useful when the file is not on this
        machine (a PostgreSQL database, or a host-mounted directory).

        Returns the new database URL, or ``None`` when the device is unknown or
        its database is not a local SQLite file. Raises ``ValueError`` when the
        target name is empty or already used by another device's file.
        """
        # A human name ("Purifier Soggiorno // 2") is normalized first: whitespace
        # and separators become hyphens, then the strict validator runs as a final
        # guard so nothing unsafe can reach the filesystem.
        slug = re.sub(r"\s+", "-", new_name.strip())
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)
        slug = re.sub(r"-{2,}", "-", slug).strip("-.")
        target = self._safe_db_name(sanitize_filename_component(slug))
        if not target.endswith(".db"):
            target = f"{target}.db"

        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return None

            old_path = await self._resolve_device_db_path(device_id)
            if old_path is None:
                # Not a local SQLite database: record the name only.
                device.database_name = new_name
                await session.commit()
                logger.info(f"Device {device_id} database name set to '{new_name}' (no file)")
                return None

            new_path = self.device_db_dir / target
            if old_path == new_path:
                device.database_name = new_name
                await session.commit()
                return f"sqlite+aiosqlite:///{new_path}"

            if new_path.exists():
                raise ValueError(f"database '{target}' already exists")

            # Close the engine before touching the file: SQLite keeps the file
            # handle open for the life of the engine.
            engine = self._device_engines.pop(device_id, None)
            if engine is not None:
                await engine.dispose()
            self._device_sessions.pop(device_id, None)

            if move_file and old_path.exists():
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.rename(new_path)
                # SQLite write-ahead log and shared-memory sidecars travel with
                # the database; leaving them behind loses recent writes.
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{old_path}{suffix}")
                    if sidecar.exists():
                        sidecar.rename(Path(f"{new_path}{suffix}"))

            new_url = f"sqlite+aiosqlite:///{new_path}"
            device.database_url = new_url
            device.database_name = new_name
            await session.commit()

        self._device_db_urls[device_id] = new_url
        self.invalidate_device_cache(device_id)
        logger.info(f"Device {device_id} database renamed: {old_path.name} -> {new_path.name}")
        return new_url

    def _default_device_db_url(self, device_id: str) -> str:
        """The URL a device's database has when nothing else was configured."""
        db_path = self.device_db_dir / self._safe_db_name(device_id)
        return f"sqlite+aiosqlite:///{db_path}"

    async def list_databases(self) -> list[dict]:
        """List the device databases that exist, with their assignments.

        Reading the open engines alone listed a database only once its device had
        touched it during this process, so a database sitting on disk was missing
        from the inventory and the whole list emptied on restart. The registry is
        the source of truth; ``is_active`` says whether its engine is open now.
        """
        databases: list[dict] = []
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).order_by(DeviceRegistry.device_id)
            )
            for device in result.scalars():
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

        # Serialize engine creation for a given device so concurrent callers
        # cannot each build/assign two different engines (check-then-create race).
        lock = self._engine_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._engine_locks[device_id] = lock
        async with lock:
            # Re-check under the lock.
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
                    db_path = self.device_db_dir / self._safe_db_name(device_id)
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

    _SAFE_DB_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")

    @classmethod
    def _safe_db_name(cls, device_id: str) -> str:
        """Sanitize a device_id into a safe SQLite filename segment.

        Performance optimization: use pre-compiled regex `_SAFE_DB_NAME_RE` to
        avoid raw regex compilation overhead on every database path resolution.
        """
        sanitized = cls._SAFE_DB_NAME_RE.sub("_", device_id)
        sanitized = sanitized.strip("._")
        return f"{sanitized or 'device'}.db"

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
        # F-12: serialize per-device creation so concurrent callers cannot both
        # insert a registry row (and race on the device DB) for the same id.
        lock = self._device_locks.setdefault(device_id, asyncio.Lock())
        async with lock, await self.get_core_session() as session:
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
                # A new device may bring IPs; force the reverse index rebuild.
                self.invalidate_ip_lookup_cache()
                logger.info(f"Registered new device: {device_id} ({vendor})")
        await self.get_device_engine(device_id)

    async def apply_ip_profile(self, device_id: str, ip: str) -> str:
        """Apply a configured per-IP profile to a device (database + connection).

        Reads ``core.ip_profiles[ip]`` from config. A ``database`` override is
        assigned via :meth:`assign_device_database`; the connection type is
        stored in the device's ``extra_attributes`` (default ``auto``). Returns
        the resolved connection type as a string.
        """
        try:
            profile = get_config().core.ip_profiles.get(ip)
        except Exception:  # noqa: BLE001 - config not ready
            profile = None

        # Fast path: no per-IP profile is configured for this address, so there
        # is nothing to apply. Skip the core-DB read + commit entirely — this is
        # the common case (default config has no ``ip_profiles``) and removes a
        # DB round-trip and fsync from every intercepted request.
        if profile is None:
            return "auto"

        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return "auto"

            extra = dict(device.extra_attributes or {})
            connection = getattr(profile, "connection", None)
            if connection is not None and not extra.get("connection"):
                extra["connection"] = connection.value
            device.extra_attributes = extra
            await session.commit()
            if connection is not None:
                self._device_connection_cache[device_id] = connection.value

        conn_val = connection.value if connection else "auto"

        if profile.database:
            await self.assign_device_database(device_id, database_url=profile.database)

        if connection is not None:
            self._device_connection_cache[device_id] = conn_val

        return conn_val

    async def get_device_connection(self, device_id: str) -> str:
        """Return the stored connection type for a device (default ``auto``)."""
        if device_id in self._device_connection_cache:
            return self._device_connection_cache[device_id]
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            conn = (device.extra_attributes or {}).get("connection", "auto") if device else "auto"
            self._device_connection_cache[device_id] = conn
            return conn

    async def is_ips_bypassed(self, ip: str) -> bool:
        """Return True if the IP is in per-IP bypass mode.

        A bypassed device still transits the gateway (its traffic is seen) but
        is forwarded straight to the cloud without buffer/LLM/local match.

        Source of truth: ``ip_profiles[ip].bypass`` in the config file (the
        per-IP override can be edited there or via the dashboard/API toggle that
        persists back to config).
        """
        try:
            profile = get_config().core.ip_profiles.get(ip)
        except Exception:  # noqa: BLE001 - config not ready
            profile = None
        return bool(profile and profile.bypass)

    async def read_device_meta(self, device_id: str) -> dict | None:
        """Return the persisted device header (``device_meta``) or ``None``.

        The header is the structured per-device record written at the first LLM
        flush. A ``None`` return means the protocol has not been decided yet
        (the device is still in ``auto`` / pre-first-flush).
        """
        if device_id in self._device_meta_cache:
            cached = self._device_meta_cache[device_id]
            return dict(cached) if cached is not None else None
        async with self.device_session(device_id) as session:
            result = await session.execute(
                select(DeviceMetaRow).where(DeviceMetaRow.device_id == device_id)
            )
            row = result.scalar_one_or_none()
            meta = dict(row.meta) if row else None
            self._device_meta_cache[device_id] = meta
            return dict(meta) if meta is not None else None

    async def write_device_meta(self, device_id: str, meta: dict) -> dict:
        """Persist (or update) the device header, validating the standard shape.

        Uses :class:`core.pattern_db.schemas.DeviceMeta` to enforce the common,
        readable header syntax. Returns the canonical stored header as a dict.
        """
        validated = DeviceMeta(**meta)
        payload = validated.model_dump()
        async with self.device_session(device_id) as session:
            result = await session.execute(
                select(DeviceMetaRow).where(DeviceMetaRow.device_id == device_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(DeviceMetaRow(device_id=device_id, meta=payload))
            else:
                row.meta = payload
            await session.commit()
        self._device_meta_cache[device_id] = payload
        return dict(payload)

    async def resolve_device_protocol(self, device_id: str, ingress_default: str = "http") -> str:
        """Resolve the operational protocol for a device on ingress.

        Priority (highest first):
        1. ``ip_profiles[ip].connection`` (per-IP config override, stored in
           ``extra_attributes["connection"]``).
        2. ``device_meta.connection_mode`` (stable header written at the first
           LLM flush).
        3. ``ingress_default`` (the protocol the ingress path actually speaks,
           e.g. ``https`` for the TLS MITM handler).

        ``auto`` is treated as "not a concrete decision" and yields to the next
        source, so an unconcluded device falls back to the ingress default.
        """
        # Level 1: per-IP override from config (extra_attributes["connection"]).
        connection = await self.get_device_connection(device_id)
        if connection and connection != "auto":
            return connection

        # Level 2: stable header decided at first flush.
        meta = await self.read_device_meta(device_id)
        if meta:
            mode = meta.get("connection_mode")
            if mode and mode != "auto":
                return str(mode)

        # Level 3: what this ingress path actually speaks.
        return ingress_default

    # ── DNS discovery log ────────────────────────────────────────────────────

    #: Rows kept in ``dns_query_log``: the discovery view is a working set, not
    #: an archive, and one row is one (client, hostname) pair.
    DNS_QUERY_LOG_MAX_ROWS = 5000

    async def record_dns_queries(self, observations: list[dict]) -> int:
        """Upsert observed DNS queries: one row per (client_ip, hostname, qtype).

        One statement per batch (SQLite ``ON CONFLICT``), not a read-then-write per
        observation: a flush can carry hundreds of pairs and each one would
        otherwise cost a round trip to the database. An existing pair keeps its
        ``first_seen``, gains the hits and refreshes ``last_seen``/``status``, so
        the table grows with the number of distinct pairs, not with the number of
        queries. Writes come from the single discovery recorder.
        """
        if not observations:
            return 0
        statement = sqlite_insert(DNSQueryLog).values(_collapse_dns_observations(observations))
        statement = statement.on_conflict_do_update(
            index_elements=["client_ip", "hostname", "qtype"],
            set_={
                "hit_count": DNSQueryLog.hit_count + statement.excluded.hit_count,
                "status": statement.excluded.status,
                "last_seen": statement.excluded.last_seen,
            },
        )
        async with self.core_session() as session:
            await session.execute(statement)
        return len(observations)

    async def list_dns_queries(self, limit: int = 500) -> list[dict]:
        """Most recently seen (client, hostname) pairs, newest first."""
        async with self.core_session() as session:
            result = await session.execute(
                select(DNSQueryLog).order_by(DNSQueryLog.last_seen.desc()).limit(limit)
            )
            rows = list(result.scalars().all())
        return [
            {
                "client_ip": row.client_ip,
                "hostname": row.hostname,
                "qtype": row.qtype,
                "status": row.status,
                "hit_count": row.hit_count,
                "first_seen": _iso_utc(row.first_seen),
                "last_seen": _iso_utc(row.last_seen),
            }
            for row in rows
        ]

    async def clear_dns_queries(self) -> int:
        """Empty the discovery log and return how many rows were removed."""
        async with self.core_session() as session:
            count = (
                await session.execute(select(func.count()).select_from(DNSQueryLog))
            ).scalar_one()
            await session.execute(delete(DNSQueryLog))
        return int(count)

    async def prune_dns_queries(self, max_rows: int | None = None) -> int:
        """Drop the oldest pairs so the log stays under ``max_rows``."""
        cap = self.DNS_QUERY_LOG_MAX_ROWS if max_rows is None else int(max_rows)
        async with self.core_session() as session:
            count = (
                await session.execute(select(func.count()).select_from(DNSQueryLog))
            ).scalar_one()
            excess = int(count) - cap
            if excess <= 0:
                return 0
            oldest = (
                (
                    await session.execute(
                        select(DNSQueryLog.id).order_by(DNSQueryLog.last_seen.asc()).limit(excess)
                    )
                )
                .scalars()
                .all()
            )
            if oldest:
                await session.execute(delete(DNSQueryLog).where(DNSQueryLog.id.in_(oldest)))
        return len(oldest)

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

    async def list_device_ids(self) -> list[str]:
        """Every registered device id (one query, no per-device work)."""
        async with await self.get_core_session() as session:
            result = await session.execute(select(DeviceRegistry.device_id))
            return [row[0] for row in result.all()]

    async def list_device_ids_with_storage(self) -> list[str]:
        """Ids of devices whose own database already exists on disk.

        The buffer backend switch looks for pending pairs in each device database, and it
        must not create one just to discover it was empty: a device that never had traffic
        has no storage and nothing to carry.
        """
        found: list[str] = []
        for device_id in await self.list_device_ids():
            path = await self._resolve_device_db_path(device_id)
            if path is not None and path.exists():
                found.append(device_id)
        return found

    async def list_devices(self) -> list[dict]:
        """List all registered devices."""
        relayed = await self.count_relayed_connections()
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
                    # Already stored on the registry and populated by
                    # POST /api/devices/{device_id}/ip, but it was never returned here, so the
                    # dashboard had nothing to display.
                    "ip_addresses": list(d.ip_addresses or []),
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
                    # Connections forwarded without decryption. For a device that pins its
                    # certificate this is the only visible sign that it is alive and talking.
                    "relayed_connections": relayed.get(d.device_id, 0),
                }
                for d in result.scalars().all()
            ]

    async def get_device(self, device_id: str) -> DeviceRegistry | None:
        """Get a device from the registry, using a memory cache for performance."""
        if device_id in self._device_cache:
            return self._device_cache[device_id]
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            # Cache the result, even if it's None (to avoid repeated lookups for missing devices)
            if device:
                # We expunge the object so it can be used across sessions
                session.expunge(device)
            self._device_cache[device_id] = device
            return device

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
            self.invalidate_device_cache(device_id)
            logger.info(f"Device {device_id} switched to {mode} mode")
            return True

    async def delete_device(self, device_id: str) -> bool:
        """Remove a device and its on-disk data (per-device DB + pattern file).

        Deletes the registry row, the per-device SQLite database and the
        portable ``.ride-pattern.json`` export, then drops the in-memory
        engine/session caches for that device. Returns ``False`` if the
        device does not exist.
        """
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            await session.delete(device)
            await session.commit()

        db_path = self.device_db_dir / self._safe_db_name(device_id)
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError as e:
                logger.warning(f"Could not remove device DB {db_path}: {e}")

        pattern_file = (
            self.device_db_dir.parent
            / "patterns"
            / f"{hashlib.sha256(device_id.encode('utf-8')).hexdigest()}.ride-pattern.json"
        )
        if pattern_file.exists():
            try:
                pattern_file.unlink()
            except OSError as e:
                logger.warning(f"Could not remove pattern file {pattern_file}: {e}")

        self._device_engines.pop(device_id, None)
        self._device_sessions.pop(device_id, None)
        self._device_locks.pop(device_id, None)
        self._device_db_urls.pop(device_id, None)
        self.invalidate_device_cache(device_id)
        logger.info(f"Deleted device {device_id}")
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
            self.invalidate_device_cache(device_id)
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
            self.invalidate_device_cache(device_id)
            logger.info(f"Device {device_id} auto-switch {'enabled' if enabled else 'disabled'}")
            return True

    async def update_device_match_threshold(self, device_id: str, threshold: float) -> bool:
        """Set per-device match confidence threshold (0.0 to 1.0)."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            device.match_threshold = threshold
            await session.commit()
            self.invalidate_device_cache(device_id)
            logger.info(f"Device {device_id} match_threshold set to {threshold}")
            return True

    async def update_device_context_buffer_size(self, device_id: str, size: int) -> bool:
        """Set per-device context buffer size in bytes."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return False
            device.context_buffer_size = size
            await session.commit()
            self.invalidate_device_cache(device_id)
            logger.info(f"Device {device_id} context_buffer_size set to {size}")
            return True

    # â”€â”€ Context notes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€  # noqa: E501

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
            self.invalidate_device_cache(device_id)
            return True

    async def get_device_context_notes(self, device_id: str) -> str | None:
        """Get custom context notes for a device."""
        async with await self.get_core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            return device.llm_context_notes if device else None

    # â”€â”€ LLM Profile CRUD â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€  # noqa: E501

    async def list_llm_profiles(self) -> list[dict]:
        """List all user-saved LLM profiles."""
        async with await self.get_core_session() as session:
            result = await session.execute(select(LLMProfile).order_by(LLMProfile.name))
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
            result = await session.execute(select(LLMProfile).where(LLMProfile.name == name))
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
                select(LLMProfile).where(LLMProfile.name == data["name"])
            )
            if existing.scalar_one_or_none():
                return False
            profile = LLMProfile(
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
            result = await session.execute(select(LLMProfile).where(LLMProfile.name == name))
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
            result = await session.execute(select(LLMProfile).where(LLMProfile.name == name))
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
        self._device_connection_cache.clear()
        self._device_meta_cache.clear()
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
