"""Locally served responses never replay the vendor's framing headers (C-12)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from core import server as server_mod
from core.database import DatabaseManager, ResponseTemplate
from core.pattern_db.pattern_engine import PatternEngine

CAPTURED_HEADERS = {
    "Content-Length": "999",
    "Transfer-Encoding": "chunked",
    "Content-Encoding": "gzip",
    "Connection": "keep-alive",
    "Keep-Alive": "timeout=5",
    "content-type": "application/json",
    "X-Vendor": "acme",
    "X-Count": 3,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("learned", "served"), [(404, 404), (101, 200), ("abc", 200), (700, 200)])
async def test_engine_drops_framing_headers_and_bad_statuses(learned, served):
    # A template as loaded from the device DB: learned values, unvalidated.
    template = ResponseTemplate(
        template_id="t",
        pattern_id="p",
        status_code=learned,
        headers_template=dict(CAPTURED_HEADERS),
        body_template={"ok": True},
        field_mappings={},
    )
    response = await PatternEngine(MagicMock()).build_local_response("dev", template, {})
    assert response["status_code"] == served
    assert response["headers"] == {
        "content-type": "application/json",
        "X-Vendor": "acme",
        "X-Count": "3",
    }


class _Adapter:
    async def parse_request(self, intercepted):
        intercepted.device_id = "dev-1"
        return intercepted


class _Registry:
    def get_adapter(self, _vendor):
        return _Adapter()

    def list_vendors(self):
        return ["acme"]


@pytest_asyncio.fixture
async def serve(tmp_path, monkeypatch):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "devices",
    )
    await dm.initialize()
    orch = MagicMock()
    orch.handle_request = AsyncMock()
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", orch)
    monkeypatch.setattr(server_mod, "adapter_registry", _Registry())
    monkeypatch.setattr(server_mod, "get_traffic_selector", lambda: None)
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def get(status: int, headers: dict, body: object) -> httpx.Response:
            orch.handle_request.return_value = {
                "action": "local_response",
                "response": {"status_code": status, "headers": headers, "body": body},
            }
            return await client.get("/acme/api/status")

        yield get
    await dm.close()


@pytest.mark.asyncio
async def test_served_length_matches_the_body(serve):
    response = await serve(200, dict(CAPTURED_HEADERS), {"ok": True})
    assert response.status_code == 200  # noqa: PLR2004
    assert response.headers["content-length"] == str(len(response.content))
    assert "content-encoding" not in response.headers
    assert response.headers["x-vendor"] == "acme"
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_no_content_has_no_body(serve):
    response = await serve(204, {}, {})
    assert response.status_code == 204  # noqa: PLR2004
    assert response.content == b""
