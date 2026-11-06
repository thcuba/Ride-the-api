"""MatchRateTracker - tracks pattern match rates and local autonomy metrics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select

from core.database import MatchStats

if TYPE_CHECKING:
    from core.database import DatabaseManager


class MatchResult(StrEnum):
    """Result of a pattern match attempt."""

    LOCAL_HIT = "local_hit"  # Served from local database
    CLOUD_MISS = "cloud_miss"  # Forwarded to cloud, will learn
    ERROR = "error"  # Processing error


class MatchRateTracker:
    """Tracks real-time match hit/miss rate per device."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self._rolling_window = 1000
        self._locks: dict[str, asyncio.Lock] = {}
        self._stats_cache: dict[str, dict] = {}
        self._flush_threshold = 50

    async def _flush_cache(self, device_id: str):
        """Flush in-memory stats cache to the database."""
        cache = self._stats_cache.get(device_id)
        if not cache or cache["updates_pending"] == 0:
            return
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        async with lock, self.db_manager.device_session(device_id) as session:
            result_obj = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result_obj.scalar_one_or_none()
            if not stats:
                stats = MatchStats(
                    device_id=device_id,
                    total_requests=0,
                    local_hits=0,
                    cloud_misses=0,
                    errors=0,
                    match_rate_pct=0.0,
                    patterns_learned=0,
                    templates_created=0,
                    buffer_flushes=0,
                    current_buffer_size_bytes=0,
                )
                session.add(stats)
                await session.flush()
            stats.total_requests += cache["updates_pending"]
            stats.local_hits += cache["local_hits"]
            stats.cloud_misses += cache["cloud_misses"]
            stats.errors += cache["errors"]
            total_attempted = stats.local_hits + stats.cloud_misses
            stats.match_rate_pct = round(
                stats.local_hits / total_attempted * 100 if total_attempted > 0 else 0.0, 2
            )
            recent = list(stats.recent_results or [])
            recent.extend(cache["recent_results"])
            if len(recent) > self._rolling_window:
                recent = recent[-self._rolling_window :]
            stats.recent_results = recent
            cache["updates_pending"] = 0
            cache["local_hits"] = 0
            cache["cloud_misses"] = 0
            cache["errors"] = 0
            cache["recent_results"] = []

    async def record_result(self, device_id: str, match_result: MatchResult):
        """Record a match result and update stats."""
        cache = self._stats_cache.setdefault(
            device_id,
            {
                "updates_pending": 0,
                "local_hits": 0,
                "cloud_misses": 0,
                "errors": 0,
                "recent_results": [],
            },
        )
        cache["updates_pending"] += 1
        if match_result == MatchResult.LOCAL_HIT:
            cache["local_hits"] += 1
        elif match_result == MatchResult.CLOUD_MISS:
            cache["cloud_misses"] += 1
        else:
            cache["errors"] += 1
        cache["recent_results"].append(
            {"result": match_result.value, "timestamp": datetime.now(UTC).isoformat()}
        )
        if len(cache["recent_results"]) > self._rolling_window * 2:
            cache["recent_results"] = cache["recent_results"][-self._rolling_window :]
        if cache["updates_pending"] >= self._flush_threshold:
            await self._flush_cache(device_id)

    async def get_stats(self, device_id: str) -> dict:
        """Get current match stats for a device."""
        await self._flush_cache(device_id)
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()
            if not stats:
                return {
                    "total_requests": 0,
                    "local_hits": 0,
                    "cloud_misses": 0,
                    "match_rate_pct": 0.0,
                    "errors": 0,
                    "patterns_learned": 0,
                    "buffer_flushes": 0,
                    "current_buffer_size_bytes": 0,
                    "recent_results": [],
                }
            return {
                "total_requests": stats.total_requests,
                "local_hits": stats.local_hits,
                "cloud_misses": stats.cloud_misses,
                "errors": stats.errors,
                "match_rate_pct": stats.match_rate_pct,
                "patterns_learned": stats.patterns_learned,
                "templates_created": stats.templates_created,
                "buffer_flushes": stats.buffer_flushes,
                "current_buffer_size_bytes": stats.current_buffer_size_bytes,
                "last_flush_at": stats.last_flush_at.isoformat() if stats.last_flush_at else None,
                "recent_results": (stats.recent_results or [])[-100:],
            }
