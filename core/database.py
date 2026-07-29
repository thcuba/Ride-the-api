"""
Core Database Architecture - Multi-Vendor SQL Databases
Core DB + Per-Vendor DB (SQLite default, PostgreSQL optional)
"""

from __future__ import annotations

import contextlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncGenerator, Optional

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
# CORE DATABASE MODELS (shared across all vendors)
# ═══════════════════════════════════════════════════════════════════════════════

class DeviceRegistry(Base):
    """Core device registry - maps device_id to vendor DB."""
    __tablename__ = "device_registry"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(32), index=True, nullable=False)  # protocol/vendor identifier
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)  # ac, heat_pump, ventilator
        vendor_db_name: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g., "example", "my_protocol"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # vendor-specific caps
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)      # vendor-specific config
    status: Mapped[str] = mapped_column(String(32), default="online", nullable=False)
    last_seen: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ModelRegistry(Base):
    """Core model registry - tracks models per vendor."""
    __tablename__ = "model_registry"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    vendor: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)  # onnx, tflite, tensorrt
    model_path: Mapped[str] = mapped_column(String(512), nullable=False)  # path in vendor DB
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # accuracy, latency, etc.
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GlobalPolicy(Base):
    """Global policies applied to all vendors."""
    __tablename__ = "global_policies"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)  # energy_cap, comfort_range, custom
    applies_to: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # vendor, device_type filters
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(default=100, nullable=False)  # lower = higher priority
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CloudProvider(Base):
    """Cloud provider configuration for fallback."""
    __tablename__ = "cloud_providers"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    provider_class: Mapped[str] = mapped_column(String(128), nullable=False)  # full import path
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # api_keys, endpoints, etc.
    is_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    priority: Mapped[int] = mapped_column(default=1, nullable=False)  # fallback order
    health_check_interval: Mapped[int] = mapped_column(default=60, nullable=False)  # seconds
    last_health_check: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_healthy: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ═══════════════════════════════════════════════════════════════════════════════
# PER-VENDOR DATABASE MODELS (template - each vendor gets their own DB with these tables)
# ═══════════════════════════════════════════════════════════════════════════════

class VendorDevice(Base):
    """Per-vendor device table - stored in vendor DB."""
    __tablename__ = "devices"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    firmware_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_seen: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class VendorReading(Base):
    """Per-vendor sensor readings - time series data."""
    __tablename__ = "readings"
    __table_args__ = (
        Index("ix_readings_device_ts", "device_id", "timestamp"),
        Index("ix_readings_ts", "timestamp"),
    )
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    timestamp: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    
    # HVAC Standard Fields
    temp_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_outdoor: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)  # cool, heat, fan, auto, dry
    fan_speed: Mapped[str | None] = mapped_column(String(32), nullable=True)  # low, medium, high, auto
    swing_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    
    # Vendor-specific extensions (JSON for flexibility)
    vendor_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    
    # Metadata
    source: Mapped[str] = mapped_column(String(32), default="device", nullable=False)  # device, cloud, edge
    quality: Mapped[str] = mapped_column(String(16), default="good", nullable=False)  # good, estimated, stale


class VendorCommand(Base):
    """Per-vendor command log."""
    __tablename__ = "commands"
    __table_args__ = (
        Index("ix_commands_device_ts", "device_id", "timestamp"),
        Index("ix_commands_status", "status"),
    )
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    timestamp: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    command: Mapped[str] = mapped_column(String(64), nullable=False)  # set_temp, set_mode, set_fan, on, off
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    
    # Source tracking
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # edge_auto, edge_manual, cloud_app, cloud_schedule
    edge_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # which model generated this
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    
    # Execution tracking
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)  # pending, sent, acked, failed, timeout
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VendorModel(Base):
    """Per-vendor trained models metadata."""
    __tablename__ = "models"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    model_path: Mapped[str] = mapped_column(String(512), nullable=False)  # path to ONNX/TFLite file
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    
    # Training metadata
    training_data_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    training_data_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    training_samples: Mapped[int] = mapped_column(default=0, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # MAE, RMSE, accuracy per action
    
    # Deployment
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_canary: Mapped[bool] = mapped_column(default=False, nullable=False)
    canary_traffic_pct: Mapped[float] = mapped_column(default=0.0, nullable=False)
    deployed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VendorPolicy(Base):
    """Per-vendor control policies."""
    __tablename__ = "policies"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)  # pid, rl, rule_based, schedule
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class VendorInterceptedRequest(Base):
    """Raw intercepted requests for training data."""
    __tablename__ = "intercepted_requests"
    __table_args__ = (
        Index("ix_intercepted_device_ts", "device_id", "timestamp"),
    )
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    timestamp: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    # Request details
    method: Mapped[str] = mapped_column(String(16), nullable=False)  # GET, POST, MQTT_PUB, etc.
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    
    # Response details
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    
    # Processing
    processed: Mapped[bool] = mapped_column(default=False, nullable=False)
    processed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edge_action: Mapped[str | None] = mapped_column(String(64), nullable=True)  # responded_locally, forwarded, blocked
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class DatabaseManager:
    """Manages core DB + per-vendor DBs."""
    
    def __init__(
        self,
        core_db_url: str,
        vendor_db_dir: Path,
        vendor_db_urls: dict[str, str] | None = None,
        echo: bool = False,
    ):
        self.core_db_url = core_db_url
        self.vendor_db_dir = Path(vendor_db_dir)
        self.vendor_db_dir.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        
        self._core_engine: AsyncEngine | None = None
        self._core_session_factory: async_sessionmaker[AsyncSession] | None = None
        
        self._vendor_engines: dict[str, AsyncEngine] = {}
        self._vendor_sessions: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._vendor_db_urls = vendor_db_urls or {}
    
    async def initialize(self) -> None:
        """Initialize all databases."""
        # Core DB
        self._core_engine = create_async_engine(
            self.core_db_url,
            echo=self.echo,
            poolclass=NullPool,
        )
        self._core_session_factory = async_sessionmaker(
            self._core_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        async with self._core_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        logger.info("Core database initialized")
    
    async def get_vendor_engine(self, vendor: str) -> AsyncEngine:
        """Get or create vendor database engine."""
        if vendor in self._vendor_engines:
            return self._vendor_engines[vendor]
        
        # Use custom URL or default SQLite in vendor_db_dir
        if vendor in self._vendor_db_urls:
            db_url = self._vendor_db_urls[vendor]
        else:
            db_path = self.vendor_db_dir / f"{vendor}.db"
            db_url = f"sqlite+aiosqlite:///{db_path}"
        
        engine = create_async_engine(
            db_url,
            echo=self.echo,
            poolclass=NullPool,
        )
        
        # Create tables for vendor DB
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        self._vendor_engines[vendor] = engine
        self._vendor_sessions[vendor] = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        logger.info(f"Vendor database '{vendor}' initialized at {db_url}")
        return engine
    
    def get_vendor_session(self, vendor: str) -> async_sessionmaker[AsyncSession]:
        """Get session factory for vendor DB."""
        if vendor not in self._vendor_sessions:
            raise ValueError(f"Vendor DB '{vendor}' not initialized. Call get_vendor_engine first.")
        return self._vendor_sessions[vendor]
    
    @contextlib.asynccontextmanager
    async def core_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get core database session."""
        if not self._core_session_factory:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        async with self._core_session_factory() as session:
            yield session
    
    @contextlib.asynccontextmanager
    async def vendor_session(self, vendor: str) -> AsyncGenerator[AsyncSession, None]:
        """Get vendor database session."""
        session_factory = self.get_vendor_session(vendor)
        async with session_factory() as session:
            yield session
    
    async def close(self) -> None:
        """Close all database connections."""
        if self._core_engine:
            await self._core_engine.dispose()
        for engine in self._vendor_engines.values():
            await engine.dispose()
        logger.info("All database connections closed")


# ═══════════════════════════════════════════════════════════════════════════════
# VENDOR DB ABSTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

class VendorDatabase(ABC):
    """Abstract base for vendor-specific database operations."""
    
    def __init__(self, vendor: str, db_manager: DatabaseManager):
        self.vendor = vendor
        self.db_manager = db_manager
    
    @property
    @abstractmethod
    def device_model(self) -> type[Base]:
        """Vendor-specific device model (can extend VendorDevice)."""
        pass
    
    @property
    @abstractmethod
    def reading_model(self) -> type[Base]:
        """Vendor-specific reading model (can extend VendorReading)."""
        pass
    
    @property
    @abstractmethod
    def command_model(self) -> type[Base]:
        """Vendor-specific command model (can extend VendorCommand)."""
        pass
    
    async def get_device(self, device_id: str) -> VendorDevice | None:
        """Get device by ID."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            result = await session.execute(
                select(self.device_model).where(self.device_model.device_id == device_id)
            )
            return result.scalar_one_or_none()
    
    async def upsert_device(self, device_data: dict) -> VendorDevice:
        """Insert or update device."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            device = await self.get_device(device_data["device_id"])
            if device:
                for key, value in device_data.items():
                    setattr(device, key, value)
                device.updated_at = func.now()
            else:
                device = self.device_model(**device_data)
                session.add(device)
            await session.commit()
            await session.refresh(device)
            return device
    
    async def add_reading(self, reading_data: dict) -> VendorReading:
        """Add a sensor reading."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            reading = self.reading_model(**reading_data)
            session.add(reading)
            await session.commit()
            return reading
    
    async def add_command(self, command_data: dict) -> VendorCommand:
        """Log a command."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            command = self.command_model(**command_data)
            session.add(command)
            await session.commit()
            return command
    
    async def get_recent_readings(
        self,
        device_id: str,
        limit: int = 100,
        since: DateTime | None = None,
    ) -> list[VendorReading]:
        """Get recent readings for a device."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            query = (
                select(self.reading_model)
                .where(self.reading_model.device_id == device_id)
                .order_by(self.reading_model.timestamp.desc())
                .limit(limit)
            )
            if since:
                query = query.where(self.reading_model.timestamp >= since)
            result = await session.execute(query)
            return list(result.scalars().all())
    
    async def get_active_model(self) -> VendorModel | None:
        """Get currently active model for this vendor."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            result = await session.execute(
                select(VendorModel)
                .where(VendorModel.vendor == self.vendor)
                .where(VendorModel.is_active == True)
                .order_by(VendorModel.deployed_at.desc())
            )
            return result.scalar_one_or_none()
    
    async def log_intercepted_request(self, request_data: dict) -> VendorInterceptedRequest:
        """Log intercepted request for training."""
        async with self.db_manager.vendor_session(self.vendor) as session:
            req = VendorInterceptedRequest(**request_data)
            session.add(req)
            await session.commit()
            return req