"""ContextBuffer, CorrelatedPair, and FlushCoordinator for captured device traffic."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, select

from core.buffer import create_buffer_store
from core.database import LLMContextBuffer

if TYPE_CHECKING:
    from core.buffer.store import BufferStore
    from core.database import DatabaseManager


@dataclass
class CorrelatedPair:
    """A correlated request/response pair ready for buffer and analysis."""

    pair_id: str

    device_id: str

    vendor: str

    protocol: str

    method: str

    path: str

    request_headers: dict

    request_body: Any

    request_query: dict

    response_status: int

    response_headers: dict

    response_body: Any

    latency_ms: float

    correlation_confidence: float

    timestamp: datetime

    enrichment: dict | None = None


class ContextBuffer:
    """Sliding-window context buffer per device.







    A thin wrapper over a :class:`BufferStore`; the store decides whether the



    buffered pairs live on durable storage (``disk``) or in-process RAM



    (``memory``), selected at construction time via the current runtime



    backend.



    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        max_size_bytes: int = 524288,
        store: BufferStore | None = None,
    ) -> None:

        self.db_manager = db_manager

        self.max_size_bytes = max_size_bytes

        self.store: BufferStore = store or create_buffer_store(db_manager)

    def rebind_store(self, store: BufferStore) -> None:
        """Point this buffer at another store (used when the backend switches).







        Rebinding beats rebuilding the buffer: the pairs already in flight stay in the



        buffer they were carried into, and the next pair lands in the new backend.



        """

        self.store = store

    async def add_pair(self, device_id: str, pair: CorrelatedPair) -> int:
        """Add a pair and return the device's current buffered byte size."""

        pair_json = {
            "pair_id": pair.pair_id,
            "protocol": pair.protocol,
            "method": pair.method,
            "path": pair.path,
            "request_headers": pair.request_headers,
            "request_body": pair.request_body,
            "request_query": pair.request_query,
            "response_status": pair.response_status,
            "response_headers": pair.response_headers,
            "response_body": pair.response_body,
            "latency_ms": pair.latency_ms,
            "timestamp": pair.timestamp.isoformat(),
        }

        if pair.enrichment:
            pair_json["enrichment"] = pair.enrichment

        serialized = json.dumps(pair_json)

        estimated_size = len(serialized)

        return await self.store.add_pair(device_id, pair_json, estimated_size)

    async def get_buffer_pairs(self, device_id: str) -> list[dict]:
        """Get all unflushed buffer entries for a device."""

        return await self.store.get_buffer_pairs(device_id)

    async def flush(self, device_id: str) -> int:
        """Mark all buffer entries for a device as flushed and clear buffer size."""

        return await self.store.flush(device_id)

    async def flush_selected(self, device_id: str, entry_ids: list[int]) -> int:
        """Mark only the specified buffer entries as flushed."""

        return await self.store.flush_selected(device_id, entry_ids)

    async def delete_entry(self, device_id: str, entry_id: int) -> bool:
        """Delete a single buffer entry."""

        return await self.store.delete_entry(device_id, entry_id)

    async def clear_cache(self, device_id: str):
        """Clear session cache for a device after flush."""

        await self.store.clear_cache(device_id)

    async def get_current_size(self, device_id: str) -> int:
        """Get current buffer size for a device."""

        return await self.store.get_current_size(device_id)

    async def prune(
        self, device_id: str, max_pairs_per_device: int = 10000, pair_ttl_hours: int = 168
    ) -> int:
        """Delete persisted buffer rows that are too old or exceed the cap.







        Applies ``pair_ttl_hours`` to a device's flushed rows and caps the



        retained pairs per device at ``max_pairs_per_device`` so the on-disk



        training store cannot grow without bound. Returns rows deleted.



        """

        cutoff = datetime.now(UTC) - timedelta(hours=pair_ttl_hours)

        total = 0

        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                delete(LLMContextBuffer).where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed.is_(True),
                        LLMContextBuffer.flushed_at.is_not(None),
                        LLMContextBuffer.flushed_at < cutoff,
                    )
                )
            )

            total += result.rowcount or 0

            retained = await session.execute(
                select(LLMContextBuffer.sequence)
                .where(
                    and_(
                        LLMContextBuffer.device_id == device_id,
                        LLMContextBuffer.flushed.is_(True),
                    )
                )
                .order_by(LLMContextBuffer.sequence.desc())
                .offset(max_pairs_per_device)
                .limit(1)
            )

            oldest_kept = retained.scalar_one_or_none()

            if oldest_kept is not None:
                result = await session.execute(
                    delete(LLMContextBuffer).where(
                        and_(
                            LLMContextBuffer.device_id == device_id,
                            LLMContextBuffer.flushed.is_(True),
                            LLMContextBuffer.sequence <= oldest_kept,
                        )
                    )
                )

                total += result.rowcount or 0

        return total
