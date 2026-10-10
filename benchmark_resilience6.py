import asyncio
import time
from sqlalchemy import select, func

from core.database import DatabaseManager, RequestPattern, ResponseTemplate, MatchStats, DeviceRegistry, Base
from core.resilience import CloudIndependenceVerifier

async def main():
    dm = DatabaseManager("sqlite+aiosqlite:///core.db", "./device_dbs_bench")

    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///core.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await dm.initialize()

    device_id = "test-device"
    await dm.get_or_create_device(device_id, "generic", "sensor", "Test Device")

    async with dm.device_session(device_id) as session:
        for i in range(100):
            session.add(RequestPattern(pattern_id=f"p{i}", method="GET", path_pattern=f"/api/{i}", protocol="http", intent="test"))
            session.add(ResponseTemplate(template_id=f"t{i}", pattern_id=f"p{i}", status_code=200, confidence=0.9))
        await session.commit()

    async with dm.core_session() as session:
        session.add(MatchStats(device_id=device_id, total_requests=1000, match_rate_pct=99.5))
        await session.commit()

    verifier = CloudIndependenceVerifier(dm)

    # Warmup
    for _ in range(10):
        await verifier.check_cloud_independence(device_id)

    # We can modify check_cloud_independence to cache the stats in CloudIndependenceVerifier directly since it's instantiated per API call but the background scheduler uses a long-lived one!

    # Let's write the modified benchmark showing how the cache would perform
    # Note: verifier can use TTLCache or DatabaseManager's caches. Let's add a cache to verifier for benchmarking.
    class FastCloudIndependenceVerifier(CloudIndependenceVerifier):
        def __init__(self, db_manager):
            super().__init__(db_manager)
            from cachetools import TTLCache
            self._stats_cache = TTLCache(maxsize=1024, ttl=60)

        async def check_cloud_independence(self, device_id: str) -> dict:
            cached = self._stats_cache.get(device_id)
            if cached:
                patterns_count, templates_count, max_template_confidence = cached
            else:
                async with self.db_manager.device_session(device_id) as session:
                    patterns_count_res = await session.execute(select(func.count(RequestPattern.id)))
                    patterns_count = patterns_count_res.scalar_one() or 0

                    templates_count_res = await session.execute(
                        select(
                            func.count(ResponseTemplate.id),
                            func.max(ResponseTemplate.confidence),
                        ).where(ResponseTemplate.confidence >= 0.7)
                    )
                    tmpl_row = templates_count_res.one()
                    templates_count = tmpl_row[0] or 0
                    max_template_confidence = tmpl_row[1] or 0.0
                self._stats_cache[device_id] = (patterns_count, templates_count, max_template_confidence)

            # Core session part...
            async with self.db_manager.core_session() as session:
                result = await session.execute(
                    select(MatchStats).where(MatchStats.device_id == device_id)
                )
                stats = result.scalar_one_or_none()

                result = await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
                )
                device = result.scalar_one_or_none()
                if not device:
                    return {"independent": False, "reason": "device_not_found"}

            match_rate = stats.match_rate_pct if stats else 0.0
            total_requests = stats.total_requests if stats else 0
            has_patterns = patterns_count > 0
            has_templates = templates_count > 0
            has_high_confidence = max_template_confidence >= 0.85
            has_excellent_match_rate = match_rate >= 99.0
            is_learning = device.mode == "learning"
            is_production = device.mode == "production"

            if is_production and has_patterns and has_templates and has_high_confidence:
                return {
                    "independent": True,
                    "patterns_learned": patterns_count,
                    "templates_created": templates_count,
                    "match_rate": match_rate,
                    "total_requests": total_requests,
                    "mode": "production",
                    "auto_switch_enabled": device.auto_switch_enabled,
                    "reason": "Device is in production mode with learned patterns",
                }
            return {"independent": False}

    fast_verifier = FastCloudIndependenceVerifier(dm)

    start = time.time()
    for _ in range(1000):
        await fast_verifier.check_cloud_independence(device_id)
    end = time.time()
    print(f"Fast approach: {(end-start)*1000:.2f}ms")

    await dm.close()

if __name__ == "__main__":
    asyncio.run(main())
