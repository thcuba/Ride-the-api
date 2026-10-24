"""Proxy fidelity: a forward must be truthful and byte-faithful.

Regression tests for the defects where the gateway answered success for
requests it never served, and where a vendor response was reinterpreted
(status/headers dropped, non-JSON payloads re-encoded as JSON).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from adapters.base import CommandResult, InterceptedRequest, ProtocolType
from core import cloud_forward
from core import server as server_mod
from core.cloud_forward import _raw_body_matches_parsed, _request_body_bytes
from core.server import app

# A public address so the SSRF guard does not filter it out.
_PUBLIC_IP = "93.184.216.34"


def _req(**kwargs) -> InterceptedRequest:
    base = {
        "device_id": "dev",
        "timestamp": 0,
        "protocol": ProtocolType.HTTPS,
        "method": "POST",
        "path": "/x",
        "headers": {},
    }
    base.update(kwargs)
    return InterceptedRequest(**base)


class TestRequestPayloadFidelity:
    """The device's bytes go upstream untouched unless a rule rewrote them."""

    def test_non_json_body_is_replayed_verbatim(self):
        raw = b"<command><set>240</set></command>"
        headers: dict[str, str] = {"content-type": "application/xml"}
        body = _request_body_bytes(_req(body=None, body_bytes=raw), "POST", headers)
        assert body == raw
        assert headers["content-length"] == str(len(raw))
        # The original content-type must survive: re-encoding was the bug.
        assert headers["content-type"] == "application/xml"

    def test_untouched_json_body_is_replayed_byte_identical(self):
        raw = b'{"a": 1,  "b": [2, 3]}'  # note the unusual spacing
        headers: dict[str, str] = {"content-type": "application/json"}
        body = _request_body_bytes(
            _req(body={"a": 1, "b": [2, 3]}, body_bytes=raw), "POST", headers
        )
        assert body == raw

    def test_modified_body_is_reserialised(self):
        raw = b'{"a": 1}'
        headers: dict[str, str] = {}
        body = _request_body_bytes(_req(body={"a": 2}, body_bytes=raw), "POST", headers)
        assert body == b'{"a": 2}'
        assert headers["content-type"] == "application/json"

    def test_bodyless_methods_send_no_body(self):
        headers: dict[str, str] = {}
        assert _request_body_bytes(_req(body={"a": 1}, body_bytes=b"{}"), "GET", headers) is None
        assert _request_body_bytes(_req(body={"a": 1}, body_bytes=b"{}"), "HEAD", headers) is None

    def test_raw_matcher_handles_garbage(self):
        assert _raw_body_matches_parsed(None, b"anything") is True
        assert _raw_body_matches_parsed({"a": 1}, b"not json") is False
        assert _raw_body_matches_parsed({"a": 1}, b'{"a": 1}') is True


class TestForwardResponseFidelity:
    """Status, headers and raw bytes of the vendor response are preserved."""

    async def test_forward_propagates_upstream_response(self, monkeypatch):
        forwarded: dict = {}
        upstream = cloud_forward.ForwardedResponse(
            status_code=429,
            headers={"retry-after": "30", "content-type": "text/plain"},
            body=b"slow down",
        )

        async def fake_resolve(_self, *_args):
            return [_PUBLIC_IP]

        async def fake_forward(_self, **kwargs):
            forwarded.update(kwargs)
            return upstream

        monkeypatch.setattr(cloud_forward.CloudForwarder, "resolve", fake_resolve)
        monkeypatch.setattr(cloud_forward.CloudForwarder, "forward", fake_forward)

        result = await cloud_forward.forward_intercepted(
            _req(body={"a": 1}, body_bytes=b'{"a": 1}'),
            hostname="api.example.com",
        )

        assert result.success is True
        # A rate limit must reach the device as a rate limit, not as "200 OK".
        assert result.status_code == 429  # noqa: PLR2004
        assert result.response_headers["retry-after"] == "30"
        assert result.body_bytes == b"slow down"
        # ... and the request that went out was the device's own payload.
        assert forwarded["body"] == b'{"a": 1}'

    def test_upstream_response_replays_status_headers_and_bytes(self):
        class _Result:
            status_code = 401
            response_headers = {
                "www-authenticate": "Bearer",
                "content-length": "9",  # upstream value: must be recomputed
                "connection": "keep-alive",  # hop-by-hop: must not be replayed
                "content-type": "text/plain",
            }
            body_bytes = b"unauthorized"
            response = {"error": "unauthorized"}

        resp = server_mod._upstream_response(_Result())

        assert resp.status_code == 401  # noqa: PLR2004
        assert resp.body == b"unauthorized"
        assert resp.headers["www-authenticate"] == "Bearer"
        assert resp.headers["content-type"] == "text/plain"
        assert resp.headers["content-length"] == str(len(b"unauthorized"))
        assert "connection" not in resp.headers


class TestPassthroughForwardsForReal:
    """A passthrough decision must reach the upstream."""

    def test_local_passthrough_forwards_instead_of_faking_success(self, monkeypatch):
        seen: dict = {}

        class FakeAdapter:
            async def forward_to_cloud(self, request):
                seen["request"] = request
                return CommandResult(
                    success=True,
                    response={"ok": True},
                    forwarded=True,
                    status_code=201,
                    response_headers={"content-type": "application/json"},
                    body_bytes=b'{"ok": true}',
                )

        class FakeRegistry:
            def get_adapter(_self, _vendor):
                return FakeAdapter()

            def list_vendors(self):
                return ["example"]

        class FakeSelector:
            def evaluate(self, _info):
                return MagicMock(value="passthrough")

        class FakeDb:
            async def is_ips_bypassed(self, _ip):
                return False

        server_mod.config_manager.config.security.auth_enabled = False
        monkeypatch.setattr(
            server_mod.config_manager.config.learning, "signal_forward_to_cloud", False
        )
        monkeypatch.setattr(server_mod, "adapter_registry", FakeRegistry())
        monkeypatch.setattr(server_mod, "db_manager", FakeDb())
        monkeypatch.setattr(server_mod, "orchestrator", MagicMock())
        monkeypatch.setattr(server_mod, "get_traffic_selector", FakeSelector)
        # TestClient's client host is not an IP, so pin the locality decision.
        monkeypatch.setattr(server_mod, "_is_local_ip", lambda _ip: True)

        client = TestClient(app)
        # Send exact bytes so byte-fidelity is provable.
        raw = b'{"commands": [{"code": "on"}]}'
        resp = client.post(
            "/example/commands", content=raw, headers={"content-type": "application/json"}
        )

        # The upstream exchange happened...
        assert "request" in seen, "passthrough must forward the request upstream"
        # ... with the device's own bytes, and its real status/body came back.
        assert seen["request"].body_bytes == raw
        assert resp.status_code == 201  # noqa: PLR2004
        assert resp.json() == {"ok": True}
        assert "Traffic forwarded directly" not in resp.text
