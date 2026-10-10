import asyncio
import time
from sqlalchemy import select

from core.database import DatabaseManager, RequestPattern, ResponseTemplate, MatchStats, DeviceRegistry, Base
from core.resilience import CloudIndependenceVerifier

async def main():
    dm = DatabaseManager("sqlite+aiosqlite:///core.db", "./device_dbs_bench")

    # We need to create tables
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///core.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await dm.initialize()

    device_id = "test-device"
    await dm.get_or_create_device(device_id, "generic", "sensor", "Test Device")

    # Insert some patterns and templates
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

    start = time.time()
    for _ in range(100):
        await verifier.check_cloud_independence(device_id)
    end = time.time()

    print(f"Time for 100 iterations: {(end-start)*1000:.2f}ms")

    await dm.close()

if __name__ == "__main__":
    asyncio.run(main())
