"""Database package: core SQLite engine, per-device connection pool, and models."""

from __future__ import annotations

from core.config import get_config
from core.database.device_pool import DeviceDatabasePool
from core.database.engine import (
    _SQLITE_PRAGMAS,
    _apply_sqlite_pragmas,
    _collapse_dns_observations,
    _iso_utc,
    create_configured_engine,
)
from core.database.manager import (
    DatabaseManager,
    _db_manager,
    get_db_manager,
    init_db_manager,
)

# Models live in core/models now, but this module keeps re-exporting them: core/__init__
# and 47 test modules import them as `from core.database import X`.
from core.models import (
    Base,
    DeviceMetaRow,
    DeviceRegistry,
    DeviceState,
    DNSQueryLog,
    FieldMapping,
    InterceptedRequest,
    LLMContextBuffer,
    LLMProfile,
    MatchStats,
    ModelRegistry,
    RelayedConnection,
    RequestPattern,
    ResponseTemplate,
    SessionCache,
)

__all__ = [
    "Base",
    "DNSQueryLog",
    "DatabaseManager",
    "DeviceDatabasePool",
    "DeviceMetaRow",
    "DeviceRegistry",
    "DeviceState",
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
    "get_config",
    "get_db_manager",
    "init_db_manager",
]
