"""
Buffer Manager --- accumulates raw intercepted pairs until configurable capacity,
then flushes to the configured LLM for deciphering.

Extends the existing ContextBuffer from core/pipeline with export/import
functionality for the portable .ride-capture.json format.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select

from core.buffer import create_buffer_store
from core.database import (
    DatabaseManager,
    DeviceRegistry,
)
from core.pattern_db.schemas import (
    CaptureDB,
    CaptureMeta,
    CaptureSession,
    RawPairWithResponse,
    RawResponse,
)
from core.pattern_db.validator import ValidationError, validate_capture

if TYPE_CHECKING:
    from core.buffer.store import BufferStore

logger = logging.getLogger(__name__)


class BufferManager:
    """
    Manages the raw capture buffer for a device.

    - Accumulates correlated request/response pairs
    - Respects configurable capacity per device
    - Signals when buffer is full and needs LLM flush
    - Exports/imports .ride-capture.json for sharing
    """

    def __init__(self, db_manager: DatabaseManager, store: BufferStore | None = None) -> None:
        self.db_manager = db_manager
        self.store: BufferStore = store or create_buffer_store(db_manager)

    async def add_pair(self, device_id: str, pair: dict) -> bool:
        """Add a correlated pair to the buffer. Returns True if buffer is full."""
        serialized = json.dumps(pair, default=str)
        estimated_size = len(serialized.encode("utf-8"))

        size = await self.store.add_pair(device_id, pair, estimated_size)
        return size >= await self._get_max_buffer_size(device_id)

    async def get_buffer_pairs(self, device_id: str) -> list[dict]:
        """Get all unflushed buffer entries for a device."""
        return await self.store.get_buffer_pairs(device_id)

    async def flush(self, device_id: str) -> int:
        """Mark all buffer entries as flushed and reset buffer size."""
        return await self.store.flush(device_id)

    async def get_current_size(self, device_id: str) -> int:
        """Get current buffer size in bytes."""
        return await self.store.get_current_size(device_id)

    async def clear_cache(self, device_id: str):
        """Clear session cache after flush."""
        await self.store.clear_cache(device_id)
        logger.info("Cleared session cache for device %s", device_id)

    # -- Export / Import ------------------------------------------------------

    async def export_capture(
        self, device_id: str, vendor: str = "", device_type: str = ""
    ) -> CaptureDB:
        """Export buffer contents to a portable CaptureDB."""
        pairs = await self.get_buffer_pairs(device_id)
        session_pairs = []
        for p in pairs:
            pair_data = p["pair"]
            rp = RawPairWithResponse(
                pair_id=pair_data.get("pair_id", str(uuid4())),
                timestamp=pair_data.get("timestamp", datetime.now(UTC)),
                protocol=pair_data.get("protocol", "http"),
                method=pair_data.get("method", ""),
                path=pair_data.get("path", ""),
                headers=pair_data.get("request_headers", {}),
                query_params=pair_data.get("request_query", {}),
                body=pair_data.get("request_body"),
                response=RawResponse(
                    status_code=pair_data.get("response_status", 0),
                    headers=pair_data.get("response_headers", {}),
                    body=pair_data.get("response_body"),
                    latency_ms=pair_data.get("latency_ms", 0.0),
                )
                if pair_data.get("response_status")
                else None,
            )
            session_pairs.append(rp)

        return CaptureDB(
            meta=CaptureMeta(
                capture_id=f"{device_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
                vendor=vendor or "unknown",
                device_type=device_type or "unknown",
                capture_date=datetime.now(UTC),
            ),
            sessions=[
                CaptureSession(
                    session_id="export_001",
                    timestamp_start=datetime.now(UTC),
                    pairs=session_pairs,
                )
            ],
        )

    async def import_capture(self, capture: CaptureDB) -> int:
        """Import a CaptureDB into the buffer. Returns number of pairs imported."""
        # Validate against the portable JSON Schema before importing
        result = validate_capture(capture.model_dump(by_alias=True, exclude_none=True))
        if not result.valid:
            raise ValidationError(result=result)

        count = 0
        for session_data in capture.sessions:
            for pair in session_data.pairs:
                pair_dict = {
                    "pair_id": pair.pair_id,
                    "device_id": capture.device_info.device_id,
                    "vendor": capture.meta.vendor,
                    "protocol": pair.protocol,
                    "method": pair.method,
                    "path": pair.path,
                    "request_headers": pair.headers,
                    "request_body": pair.body,
                    "request_query": pair.query_params,
                    "response_status": pair.response.status_code if pair.response else None,
                    "response_headers": pair.response.headers if pair.response else {},
                    "response_body": pair.response.body if pair.response else None,
                    "latency_ms": pair.response.latency_ms if pair.response else 0.0,
                    "timestamp": pair.timestamp.isoformat(),
                }
                await self.add_pair(capture.device_info.device_id, pair_dict)
                count += 1
        return count

    # -- Internal helpers -----------------------------------------------------

    async def _get_max_buffer_size(self, device_id: str) -> int:
        """Get configured max buffer size for this device (default 512KB)."""
        try:
            async with self.db_manager.device_session(device_id) as session:
                result = await session.execute(
                    select(DeviceRegistry.context_buffer_size).where(
                        DeviceRegistry.device_id == device_id
                    )
                )
                row = result.one_or_none()
                return row[0] if row else 524288
        except Exception:
            return 524288
