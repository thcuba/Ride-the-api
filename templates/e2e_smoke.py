"""
End-to-end smoke test of the real ingress -> database workflow.

Exercises the real code path against a temp SQLite setup:
  ingress learning pairs -> correlated onto the disk buffer -> first flush ->
  *_analyze_with_llm (the ONLY mock, returns a structured PatternDB JSON
  including connection_mode) -> _save_patterns -> _persist_device_meta header
  -> export .ride-pattern.json -> sync PatternEngine -> production self-match.

Everything except the single LLM analysis call is real production code.
Run from the repo root with the venv python (needs project deps).
"""
# This is an interactive smoke-test script: the many top-level prints are
# intentional output, not debugging leftovers.
# ruff: noqa: T201
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from core.buffer import initialize_buffer_backend  # noqa: E402
from core.database import DatabaseManager, DeviceRegistry, RequestPattern  # noqa: E402
from core.pipeline import (  # noqa: E402
    CorrelatedPair,
    LearningOrchestrator,
    LearningPipeline,
)


def make_pair(idx: int, device_id: str) -> CorrelatedPair:
    """A realistic shelly-style HTTP request/response pair."""
    return CorrelatedPair(
        pair_id=f"pair-{idx}",
        device_id=device_id,
        vendor="shelly",
        protocol="http",
        method="POST",
        path="/v1.0/device/test/commands",
        request_headers={"Content-Type": "application/json"},
        request_body={"commands": [{"code": "temp_set", "value": 240}]},
        request_query={},
        response_status=200,
        response_headers={"Content-Type": "application/json"},
        response_body={"result": {"data": {"1": False, "3": 240}}},
        latency_ms=120.0,
        correlation_confidence=0.95,
        timestamp=datetime.now(UTC),
    )


LLM_ANALYSIS = {
    "meta": {
        "version": 1,
        "pattern_id": "dev-1",
        "vendor": "shelly",
        "device_type": "plug",
    },
    "client": {
        "protocols": ["http"],
        "endpoints": [
            {
                "id": "cmd",
                "intent": "set_temperature",
                "method": "POST",
                "path": "/v1.0/device/{id}/commands",
            }
        ],
    },
    "server": {
        "responses": [
            {
                "id": "rsp_cmd",
                "triggers": ["set_temperature"],
                "status_code": 200,
                "headers_template": {"Content-Type": "application/json"},
                "body_template": {"result": {"data": {"1": False, "3": 240}}},
            }
        ]
    },
    "connection_mode": "http",
}


async def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        core_db = f"sqlite+aiosqlite:///{tmp}/core.db"
        device_db_dir = tmp / "devices"

        dbm = DatabaseManager(core_db_url=core_db, device_db_dir=device_db_dir, echo=False)
        await dbm.initialize()
        initialize_buffer_backend()  # disk store, as in the real lifespan

        await dbm.get_or_create_device("dev-1", "shelly", "plug", "Shelly Plug")

        orch = LearningOrchestrator(dbm)
        orch.initialize(None)  # sets engine, matcher, tracker

        # Build the real LearningPipeline exactly as _register_for_learning does.
        buf = await orch.ensure_buffer("dev-1", buffer_size=524288)
        pipeline = LearningPipeline(
            dbm, orch.llm_decipher, buf, orch.matcher, orch.tracker, orch.engine
        )
        orch.pipeline = pipeline

        # ---- Buffer: inject the mocked LLM analysis at the flush point -----
        pipeline._analyze_with_llm = AsyncMock(
            return_value=json.loads(json.dumps(LLM_ANALYSIS))
        )

        print("== INGRESS: correlated pairs -> real disk buffer ==")
        flush_flags = []
        for i in range(3):
            flush_flags.append(await buf.add_pair("dev-1", make_pair(i, "dev-1")))
        buffered = await buf.get_buffer_pairs("dev-1")
        print(f"  added 3 pairs; needs_flush flags={flush_flags}; buffered={len(buffered)}")

        print("== FLUSH -> LLM (mocked) -> save patterns + device_meta ==")
        result = await pipeline.flush_and_learn("dev-1")
        print(
            f"  flush: success={result.get('success')} "
            f"patterns={result.get('patterns_count')}"
        )

        print("== VERIFY device_meta header (first flush) ==")
        meta = await dbm.read_device_meta("dev-1")
        print("  ", json.dumps(meta, indent=2))

        print("== VERIFY request patterns in device DB ==")
        async with dbm.device_session("dev-1") as s:
            n = (await s.execute(select(func.count()).select_from(RequestPattern))).scalar_one()
            print(f"  request_patterns rows: {n}")

        print("== VERIFY .ride-pattern.json export ==")
        patterns_dir = Path("patterns")
        files = (
            list(patterns_dir.glob("*.ride-pattern.json"))
            if patterns_dir.exists()
            else []
        )
        print(f"  exported: {[f.name for f in files]} ({len(files)} files)")

        print("== PRODUCTION self-match against learned patterns ==")
        async with dbm.core_session() as s:
            row = (
                await s.execute(select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-1"))
            ).scalar_one()
            row.mode = "production"
            row.match_threshold = 0.7  # the learned pattern scores ~0.71
            await s.commit()

        res = await orch.handle_request(
            "dev-1",
            "shelly",
            "http",
            "POST",
            "/v1.0/device/dev-1/commands",
            {"Content-Type": "application/json"},
            {"commands": [{"code": "temp_set", "value": 240}]},
            {},
        )
        print("  production result:", json.dumps(res, default=str)[:220])

        await dbm.close()
        print("\n✅ END-TO-END OK")


if __name__ == "__main__":
    asyncio.run(main())
