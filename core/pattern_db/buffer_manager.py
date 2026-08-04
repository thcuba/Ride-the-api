"""
Buffer Manager — accumulates raw intercepted pairs until configurable capacity,
then flushes to the configured LLM for deciphering.

Extends the existing ContextBuffer from core/pipeline with export/import
functionality for the portable .ride-capture.json format.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from core.database import DatabaseManager, LLMContextBuffer, MatchStats, SessionCache
from core.pattern_db.schemas import (
    CaptureDB,
    CaptureMeta,
    CaptureDeviceInfo,
    CaptureSession,
    RawPairWithResponse,
    RawResponse,
)
from core.pattern_db.validator import validate_capture, ValidationResult
from sqlalchemy import and_, delete, select

logger = logging.getLogger(__name__)


class BufferManager:
    """
    Manages the raw capture buffer for a device.

    - Accumulates correlated request/response pairs
    - Respects configurable capacity per device
    - Signals when buffer is full and needs LLM flush
    - Exports/imports .ride-capture.json for sharing
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self._current_sequence: dict[str, int] = {}

    async def add_pair(self, device_id: str, pair: dict) -> bool:
        """Add a correlated pair to the buffer. Returns True if buffer is full."""
        serialized = json.dumps(pair, default=str)
        estimated_size = len(serialized.encode("utf-8"))

        seq = self._current_sequence.get(device_id, 0)
        self._current_sequence[device_id] = seq + 1

        async with self.db_manager.device_session(device_id) as session:
            entry = LLMContextBuffer(
                device_id=device_id,
                correlated_pair=pair,
                estimated_size_bytes=estimated_size,
                sequence=seq,
            )
            session.add(entry)

            stats = await self._get_or_create_stats(session, device_id)
            stats.current_buffer_size_bytes += estimated_size

            if stats.current_buffer_size_bytes >= self._get_max_buffer_size(device_id, session):
                return True
            return False

    async def get_buffer_pairs(self, device_id: str) -> list[dict]:
        """Get all unflushed buffer entries for a device."""
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(LLMContextBuffer)
                .where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed == False,
                    )
                )
                .order_by(LLMContextBuffer.sequence)
            )
            return [
                {"id": e.id, "pair": e.correlated_pair, "size": e.estimated_size_bytes}
                for e in result.scalars().all()
            ]

    async def flush(self, device_id: str) -> int:
        """Mark all buffer entries as flushed and reset buffer size."""
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(LLMContextBuffer)
                .where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed == False,
                    )
                )
            )
            now = datetime.now(timezone.utc)
            count = 0
            for entry in result.scalars().all():
                entry.flushed = True
                entry.flushed_at = now
                count += 1

            stats = await self._get_or_create_stats(session, device_id)
            stats.current_buffer_size_bytes = 0
            stats.last_flush_at = now
            stats.buffer_flushes += 1

            return count

    async def get_current_size(self, device_id: str) -> int:
        """Get current buffer size in bytes."""
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()
            return stats.current_buffer_size_bytes if stats else 0

    async def clear_cache(self, device_id: str):
        """Clear session cache after flush."""
        async with self.db_manager.device_session(device_id) as session:
            await session.execute(
                delete(SessionCache).where(SessionCache.device_id == device_id)
            )
            logger.info("Cleared session cache for device %s", device_id)

    # ── Export / Import ────────────────────────────────────────────────────────

    async def export_capture(self, device_id: str, vendor: str = "",
                              device_type: str = "") -> CaptureDB:
        """Export buffer contents to a portable CaptureDB."""
        pairs = await self.get_buffer_pairs(device_id)
        session_pairs = []
        for p in pairs:
            pair_data = p["pair"]
            rp = RawPairWithResponse(
                pair_id=pair_data.get("pair_id", str(uuid4())),
                timestamp=pair_data.get("timestamp", datetime.now(timezone.utc)),
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
                ) if pair_data.get("response_status") else None,
            )
            session_pairs.append(rp)

        return CaptureDB(
            meta=CaptureMeta(
                capture_id=f"{device_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                vendor=vendor or "unknown",
                device_type=device_type or "unknown",
                capture_date=datetime.now(timezone.utc),
            ),
            sessions=[
                CaptureSession(
                    session_id="export_001",
                    timestamp_start=datetime.now(timezone.utc),
                    pairs=session_pairs,
                )
            ],
        )

    async def import_capture(self, capture: CaptureDB) -> int:
        """Import a CaptureDB into the buffer. Returns number of pairs imported."""
        # Validate against the portable JSON Schema before importing
        result = validate_capture(capture.model_dump(by_alias=True, exclude_none=True))
        if not result.valid:
            from core.pattern_db.validator import ValidationError
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

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_max_buffer_size(self, device_id: str, session) -> int:
        """Get configured max buffer size for this device (default 512KB)."""
        try:
            from core.database import DeviceRegistry
            import sqlalchemy as sa
            result = session.execute(
                sa.select(DeviceRegistry.context_buffer_size)
                .where(DeviceRegistry.device_id == device_id)
            )
            row = result.one_or_none()
            return row[0] if row else 524288
        except Exception:
            return 524288

    async def _get_or_create_stats(self, session, device_id: str):
        from core.database import MatchStats
        result = await session.execute(
            select(MatchStats).where(MatchStats.device_id == device_id)
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = MatchStats(
                device_id=device_id,
                total_requests=0, local_hits=0, cloud_misses=0, errors=0,
                match_rate_pct=0.0, patterns_learned=0, templates_created=0,
                buffer_flushes=0, current_buffer_size_bytes=0,
            )
            session.add(stats)
            await session.flush()
        return stats