"""Common interface for the transient per-device capture buffer stores."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BufferStore(ABC):
    """Backend for the transient per-device capture buffer.

    Implementations decide where the buffered pairs and the byte accounting
    live: on durable storage (device SQLite/Postgres file DB) or in-process
    RAM (shared in-memory SQLite engine).
    """

    @abstractmethod
    async def add_pair(self, device_id: str, pair: dict, estimated_size: int) -> int:
        """Store one correlated pair. Returns the new cumulative buffer size."""

    @abstractmethod
    async def get_buffer_pairs(self, device_id: str) -> list[dict]:
        """Return all unflushed entries as ``[{id, pair, size}]`` ordered by sequence."""

    @abstractmethod
    async def flush(self, device_id: str) -> int:
        """Mark all unflushed entries as flushed. Returns the number flushed."""

    @abstractmethod
    async def flush_selected(self, device_id: str, entry_ids: list[int]) -> int:
        """Mark only the given entry ids as flushed. Returns the number flushed."""

    @abstractmethod
    async def delete_entry(self, device_id: str, entry_id: int) -> bool:
        """Delete a single buffer entry. Returns True if it existed."""

    @abstractmethod
    async def get_current_size(self, device_id: str) -> int:
        """Return the current buffer size in bytes for the device."""

    @abstractmethod
    async def clear_cache(self, device_id: str) -> None:
        """Clear the per-device session cache."""

    @abstractmethod
    async def close(self) -> None:
        """Release any resources held by the store."""
