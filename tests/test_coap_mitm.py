"""Tests for CoAP bidirectional MITM proxy, upstream forwarding, and response correlation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aiocoap
import pytest
from aiocoap import GET, POST, Message
from aiocoap.numbers.contentformat import ContentFormat

from core.config import CoAPServerConfig
from core.protocol_servers.coap_server import CoAPServerPlugin, _CoAPRoot

if TYPE_CHECKING:
    from adapters.base import InterceptedRequest


class _Remote:
    sockaddr = ("::ffff:192.168.1.55", 5683, 0, 0)


def _coap_msg(
    code: Any,
    *path: str,
    payload: bytes = b"",
    uri_host: str | None = None,
) -> Message:
    msg = Message(code=code, uri_path=path, payload=payload)
    msg.remote = _Remote()
    if uri_host:
        msg.opt.uri_host = uri_host
    return msg


class _MockClientRequest:
    def __init__(self, response_msg: Message) -> None:
        self._response = response_msg

    @property
    async def response(self) -> Message:
        return self._response


class _MockClientContext:
    def __init__(self, response_msg: Message) -> None:
        self.sent_messages: list[Message] = []
        self._response = response_msg

    def request(self, msg: Message) -> _MockClientRequest:
        self.sent_messages.append(msg)
        return _MockClientRequest(self._response)

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_coap_mitm_forwarding_and_learning() -> None:
    """Test CoAP MITM forwarding to upstream cloud and learning response correlation."""
    correlated: list[tuple[dict, Any]] = []

    def mock_resp_handler(res: dict, body: Any, status_code: int = 200) -> None:  # noqa: ARG001
        correlated.append((res, body))

    recorded_requests: list[InterceptedRequest] = []

    async def mock_handler(req: InterceptedRequest) -> dict:
        recorded_requests.append(req)
        return {
            "action": "forward",
            "correlation_key": "coap-corr-1",
            "_learning_context": {
                "device_id": req.device_id,
                "vendor": "coap_device",
                "protocol": "coap",
            },
        }

    cfg = CoAPServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url="coap://cloud.coap-vendor.com/v1/telemetry",
    )
    plugin = CoAPServerPlugin(cfg, handler=mock_handler, response_handler=mock_resp_handler)

    upstream_reply_msg = Message(
        code=aiocoap.CONTENT,
        payload=b'{"cloud_ack": true, "status": "ok"}',
    )
    upstream_reply_msg.opt.content_format = ContentFormat.JSON
    mock_client_ctx = _MockClientContext(upstream_reply_msg)
    plugin._client_context = mock_client_ctx  # noqa: SLF001

    root = _CoAPRoot(plugin)
    req_msg = _coap_msg(GET, "api", "telemetry")
    response = await root.render(req_msg)

    assert response.code == aiocoap.CONTENT
    data = json.loads(response.payload.decode("utf-8"))
    assert data == {"cloud_ack": True, "status": "ok"}

    # Verify upstream received the forward message
    assert len(mock_client_ctx.sent_messages) == 1
    sent_msg = mock_client_ctx.sent_messages[0]
    assert sent_msg.opt.uri_host == "cloud.coap-vendor.com"
    assert sent_msg.opt.uri_path == ("v1", "telemetry")

    # Verify pipeline correlation
    assert len(correlated) == 1
    res_info, resp_body = correlated[0]
    assert res_info["correlation_key"] == "coap-corr-1"
    assert resp_body == {"cloud_ack": True, "status": "ok"}


@pytest.mark.asyncio
async def test_coap_mitm_local_response_takes_precedence() -> None:
    """When a local pattern exists, serve CoAP response locally without upstream call."""

    async def mock_handler(req: InterceptedRequest) -> dict:  # noqa: ARG001
        return {
            "action": "local_response",
            "response": {"body": {"local": True, "mode": "production"}},
        }

    cfg = CoAPServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url="coap://cloud.example.org",
    )
    plugin = CoAPServerPlugin(cfg, handler=mock_handler)
    mock_client_ctx = _MockClientContext(Message(code=aiocoap.CONTENT))
    plugin._client_context = mock_client_ctx  # noqa: SLF001

    root = _CoAPRoot(plugin)
    response = await root.render(_coap_msg(GET, "sensor", "status"))

    assert response.code == aiocoap.CONTENT
    data = json.loads(response.payload.decode("utf-8"))
    assert data == {"local": True, "mode": "production"}

    # Upstream client context should NOT be called
    assert len(mock_client_ctx.sent_messages) == 0


@pytest.mark.asyncio
async def test_coap_mitm_auto_upstream_from_uri_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test auto-resolving upstream CoAP cloud endpoint from request Uri-Host option."""

    async def mock_resolve_upstream(hostname: str) -> list[str]:
        if hostname == "coap.cloud-vendor.org":
            return ["198.51.100.42"]
        return []

    monkeypatch.setattr(
        "core.protocol_servers.coap_server.resolve_upstream",
        mock_resolve_upstream,
    )

    async def mock_handler(req: InterceptedRequest) -> dict:  # noqa: ARG001
        return {"action": "forward", "correlation_key": "auto-coap-key"}

    cfg = CoAPServerConfig(
        host="127.0.0.1",
        port=0,
        enabled=True,
        upstream_url=None,
        auto_upstream=True,
    )
    plugin = CoAPServerPlugin(cfg, handler=mock_handler)
    mock_client_ctx = _MockClientContext(
        Message(code=aiocoap.CHANGED, payload=b'{"updated": true}')
    )
    plugin._client_context = mock_client_ctx  # noqa: SLF001

    root = _CoAPRoot(plugin)
    req_msg = _coap_msg(
        POST,
        "device",
        "state",
        payload=b'{"on": true}',
        uri_host="coap.cloud-vendor.org",
    )
    response = await root.render(req_msg)

    assert response.code == aiocoap.CHANGED
    assert json.loads(response.payload.decode("utf-8")) == {"updated": True}
    assert len(mock_client_ctx.sent_messages) == 1
    assert mock_client_ctx.sent_messages[0].opt.uri_host == "coap.cloud-vendor.org"
    assert mock_client_ctx.sent_messages[0].opt.uri_path == ("device", "state")
