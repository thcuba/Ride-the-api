"""Device and registry ORM models."""

from __future__ import annotations

from sqlalchemy import JSON, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class DeviceRegistry(Base):
    """Core device registry - maps device_id to its protocol DB."""

    __tablename__ = "device_registry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ip_addresses: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    database_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="learning", nullable=False)
    match_threshold: Mapped[float] = mapped_column(Float, default=0.85, nullable=False)
    auto_switch_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    llm_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    llm_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_profile_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra_attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    context_buffer_size: Mapped[int] = mapped_column(Integer, default=524288, nullable=False)
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


class SessionCache(Base):
    """Temporary correlation cache - cleared after each learning cycle flush."""

    __tablename__ = "session_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    correlation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    correlated: Mapped[bool] = mapped_column(default=False, nullable=False)
    correlated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    in_buffer: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
