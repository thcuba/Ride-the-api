import asyncio
import time
from sqlalchemy import select, func
from cachetools import TTLCache

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

    # The actual implementation of DatabaseManager handles caching now
    # Let's see how much faster it is to just read from TTLCache
    cache = TTLCache(maxsize=1024, ttl=60)

    start = time.time()
    for _ in range(1000):
        if device_id in cache:
            _ = cache[device_id]
        else:
            async with dm.device_session(device_id) as session:
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
            cache[device_id] = (patterns_count, templates_count, max_template_confidence)
    end = time.time()
    print(f"Cached DB queries only: {(end-start)*1000:.2f}ms")

    await dm.close()

if __name__ == "__main__":
    asyncio.run(main())
