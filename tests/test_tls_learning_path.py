"""Exercise decrypted TLS request/response correlation and LLM buffer flush."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from sqlalchemy import func, select

import core.buffer as buffer_module
import core.server as server_mod
from adapters.base import InterceptedRequest, normalize_request_body
from core.database import DatabaseManager, DeviceRegistry, LLMContextBuffer, SessionCache
from core.llm_decipher import LLMDecipherService
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
async def test_tls_path_persists_pair_and_calls_configured_llm(  # noqa: PLR0915
    tmp_path, monkeypatch
):
    """A decrypted HTTPS pair is read from SQLite and sent through the LLM client."""
    root = tmp_path / "install"
    dbm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{root}/core.db",
        device_db_dir=root / "devices",
        echo=False,
    )
    await dbm.initialize()
    llm_client = None
    try:
        # Pin the buffer to disk and to this test's temporary device DB. Do not
        # read or mutate the developer's persisted runtime backend setting.
        monkeypatch.setattr(buffer_module, "_current_backend", "disk")
        monkeypatch.setattr(
            server_mod,
            "config_manager",
            SimpleNamespace(
                config=SimpleNamespace(
                    core=SimpleNamespace(device_db_dir=str(root / "devices"))
                )
            ),
        )

        # Configure the real LLM service and its flush task profile. The HTTP
        # transport below is local and deterministic; no provider is contacted.
        profile_name = "simulated-flush-profile"
        llm_config = SimpleNamespace(
            enabled=True,
            default_profile=profile_name,
            task_profiles={"flush": profile_name},
            profiles={
                profile_name: SimpleNamespace(
                    base_url="https://llm.test/v1",
                    api_key="test-key",
                    model_id="fixture-model",
                    prompt_template="Analyze {vendor} {device_type} {device_id}:\n{pairs}",
                    enabled=True,
                    timeout=5,
                    max_retries=0,
                    max_tokens=256,
                )
            },
        )

        class _ConfigManager:
            config = SimpleNamespace(llm_decipher=llm_config)

            @staticmethod
            def register_callback(_callback) -> None:
                pass

        monkeypatch.setattr("core.llm_decipher.load_llm_settings", lambda: {})
        llm = LLMDecipherService(config_manager=_ConfigManager())
        llm_http_requests = []

        def respond_to_llm(request: httpx.Request) -> httpx.Response:
            llm_http_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "patterns": [],
                                        "protocols": ["https"],
                                        "connection_mode": "https",
                                    }
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        llm_client = httpx.AsyncClient(transport=httpx.MockTransport(respond_to_llm))
        monkeypatch.setattr(
            llm,
            "_build_client",
            lambda _profile: AsyncOpenAI(
                api_key="test-key",
                base_url="https://llm.test/v1",
                http_client=llm_client,
            ),
        )

        # A known device lets the TLS path exercise its normal IP resolution.
        await dbm.get_or_create_device("dev-1", "example", "plug", "Test Plug")
        async with dbm.core_session() as session:
            device = (
                await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == "dev-1")
                )
            ).scalar_one()
            device.ip_addresses = [CLIENT_IP]
            device.mode = "learning"
            device.context_buffer_size = 1
            await session.commit()
        dbm.invalidate_device_cache("dev-1")

        orch = LearningOrchestrator(dbm)
        orch.initialize(llm)
        buf = await orch.ensure_buffer("dev-1", buffer_size=524288)
        orch.pipeline = LearningPipeline(dbm, llm, buf, orch.tracker, orch.engine)

        monkeypatch.setattr(server_mod, "db_manager", dbm)
        monkeypatch.setattr(server_mod, "orchestrator", orch)
        monkeypatch.setattr(server_mod, "adapter_registry", _Registry(_Adapter()))

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
        assert result is not None, "the decrypted request must be processed, not dropped"
        assert result.get("action") == "forward", result
        assert result.get("_learning_context") == {
            "device_id": "dev-1",
            "vendor": "example",
            "protocol": "https",
        }

        # The request is registered durably before its upstream response arrives.
        async with dbm.device_session("dev-1") as session:
            request_rows = (
                await session.execute(
                    select(SessionCache).where(
                        SessionCache.correlation_key == result["correlation_key"]
                    )
                )
            ).scalars().all()
        assert len(request_rows) == 1
        assert request_rows[0].method == "POST"
        assert request_rows[0].path == "/v1.0/device/dev-1/commands"
        assert request_rows[0].body == DEVICE_BODY.decode()

        response_result = await server_mod.handle_tls_upstream_response(
            result,
            {
                "status_code": 200,
                "headers": {"content-type": "application/json"},
                "body": VENDOR_BODY,
            },
        )
        assert response_result is not None
        assert response_result["action"] == "buffered_for_learning"
        assert response_result["pair_id"]
        assert response_result["buffer_flushed"] is True

        # Confirm the OpenAI-compatible SDK made one request with the exact
        # correlated pair from SQLite, using the configured flush profile.
        assert len(llm_http_requests) == 1
        llm_request = llm_http_requests[0]
        assert llm_request.url.path == "/v1/chat/completions"
        payload = json.loads(llm_request.content)
        assert payload["model"] == "fixture-model"
        assert payload["response_format"] == {"type": "json_object"}
        prompt = payload["messages"][1]["content"]
        assert '"method": "POST"' in prompt
        assert '"path": "/v1.0/device/dev-1/commands"' in prompt
        assert json.dumps(DEVICE_BODY.decode()) in prompt
        assert '"response_status": 200' in prompt
        assert '"result": {' in prompt
        assert '"ok": true' in prompt

        # The same per-device SQLite DB holds the buffer row, now marked flushed.
        async with dbm.device_session("dev-1") as session:
            buffer_rows = (
                await session.execute(
                    select(LLMContextBuffer).where(LLMContextBuffer.device_id == "dev-1")
                )
            ).scalars().all()
        assert len(buffer_rows) == 1
        assert buffer_rows[0].flushed is True
        assert buffer_rows[0].correlated_pair["path"] == "/v1.0/device/dev-1/commands"
        assert await orch.pipeline.buffer.get_buffer_pairs("dev-1") == []

        async with dbm.core_session() as session:
            assert (
                await session.execute(select(func.count()).select_from(DeviceRegistry))
            ).scalar_one() == 1
    finally:
        if llm_client is not None:
            await llm_client.aclose()
        await dbm.close()
