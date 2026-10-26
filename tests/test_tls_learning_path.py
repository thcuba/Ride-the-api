"""Exercise decrypted TLS request/response correlation and LLM buffer flush."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import func, select

import core.server as server_mod
from adapters.base import InterceptedRequest, normalize_request_body
from core.buffer import initialize_buffer_backend
from core.database import DatabaseManager, DeviceRegistry, SessionCache
from core.pipeline import LearningOrchestrator, LearningPipeline
from core.tls_mitm import DecryptedRequest

CLIENT_IP = "192.168.1.77"  # the phone/app talking to the device's local API
DEVICE_BODY = b'<command><temp_set value="240"/></command>'

VENDOR_BODY = b'{"result": {"ok": true}}'


class _Adapter:
    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        return request


class _Registry:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter

    def get_adapter(self, _vendor: str) -> _Adapter:
        return self.adapter


# ── the helper itself ────────────────────────────────────────────────────────


def test_normalize_request_body_never_returns_bytes():
    """Every shape a device can send must become JSON-storable."""
    assert normalize_request_body(b"") is None
    assert normalize_request_body(b'{"a": 1}') == {"a": 1}
    assert normalize_request_body(DEVICE_BODY) == DEVICE_BODY.decode()
    assert normalize_request_body({"a": 1}) == {"a": 1}
    assert normalize_request_body(None) is None

    binary = bytes(range(256))
    encoded = normalize_request_body(binary)
    assert encoded == {
        "_encoding": "base64",
        "data": base64.b64encode(binary).decode("ascii"),
    }
    # lossless: the original bytes come back out
    assert base64.b64decode(encoded["data"]) == binary


# ── the path a real device uses ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tls_path_buffers_response_and_flushes_with_configured_llm(tmp_path, monkeypatch):
    """A decrypted HTTPS request/response pair must reach the LLM flush."""
    root = tmp_path / "install"
    dbm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{root}/core.db",
        device_db_dir=root / "devices",
        echo=False,
    )
    await dbm.initialize()
    initialize_buffer_backend()
    # A device already bound to this source IP, as automatic recognition expects.
    await dbm.get_or_create_device("dev-1", "example", "plug", "Test Plug")
    async with dbm.core_session() as session:
        device = (
            await session.execute(select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-1"))
        ).scalar_one()
        device.ip_addresses = [CLIENT_IP]
        device.mode = "learning"
        device.context_buffer_size = 1
        await session.commit()
    dbm.invalidate_device_cache("dev-1")

    orch = LearningOrchestrator(dbm)
    orch.initialize(None)

    class _ConfiguredLLM:
        @staticmethod
        def resolve_task_profile(task: str) -> str:
            assert task == "flush"
            return "simulated-flush-profile"

    llm = _ConfiguredLLM()
    orch.llm_decipher = llm
    buf = await orch.ensure_buffer("dev-1", buffer_size=524288)
    orch.pipeline = LearningPipeline(dbm, orch.llm_decipher, buf, orch.tracker, orch.engine)

    llm_calls = []

    async def analyze_with_configured_llm(context, profile_name, base_url, model_id):
        llm_calls.append((context, profile_name, base_url, model_id))
        return {"patterns": [], "protocols": ["https"], "connection_mode": "https"}

    orch.pipeline._analyze_with_llm = analyze_with_configured_llm

    adapter = _Adapter()
    monkeypatch.setattr(server_mod, "db_manager", dbm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", _Registry(adapter))

    request = DecryptedRequest(
        client_ip=CLIENT_IP,
        client_port=51234,
        dst_port=8443,
        sni="api.example.com",
        method="POST",
        path="/v1.0/device/dev-1/commands",
        http_version="1.1",
        headers={"host": "api.example.com", "content-type": "application/xml"},
        body=DEVICE_BODY,
    )

    result = await server_mod.handle_tls_decrypted_request(request)

    # Before the fix this returned None (the INSERT raised) and the MITM answered
    # 502 local_response_unavailable.
    assert result is not None, "the decrypted request must be processed, not dropped"
    assert result.get("action") == "forward", result
    assert result.get("_learning_context") == {
        "device_id": "dev-1",
        "vendor": "example",
        "protocol": "https",
    }

    # The pair reached the database: the body is no longer raw bytes.
    async with dbm.device_session("dev-1") as session:
        rows = (await session.execute(select(SessionCache))).scalars().all()
    assert rows, "the request must be registered for learning on the HTTPS path"
    stored = rows[-1]
    assert not isinstance(stored.body, (bytes, bytearray)), (
        f"bytes must never reach the JSON column: {stored.body!r}"
    )
    assert stored.path == "/v1.0/device/dev-1/commands"
    assert stored.method == "POST"

    # The TLS listener owns the upstream exchange, so explicitly feed its real
    # response back into the orchestrator: that pair should trigger the small
    # test threshold and invoke the configured flush profile.
    response_result = await server_mod.handle_tls_upstream_response(
        result,
        {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": VENDOR_BODY,
        },
    )
    assert response_result == {
        "action": "buffered_for_learning",
        "pair_id": response_result["pair_id"],
        "buffer_flushed": True,
    }
    assert len(llm_calls) == 1
    assert llm_calls[0][1] == "simulated-flush-profile"
    assert llm_calls[0][0]["total_pairs"] == 1
    assert await orch.pipeline.buffer.get_buffer_pairs("dev-1") == []

    async with dbm.core_session() as session:
        assert (
            await session.execute(select(func.count()).select_from(DeviceRegistry))
        ).scalar_one() == 1

    await dbm.close()
