"""
Tests for the M10 protocol filter in PatternEngine.find_best_match.

A device may have learned endpoints across several protocols; an incoming
request carries a resolved ``protocol`` (https/mqtt/coap/...), and
``find_best_match`` must skip endpoints whose own ``protocol`` differs —
while preserving legacy endpoints (empty protocol) that match anything.

Covers:
- cached path: same protocol matches, different protocol is skipped,
  empty endpoint protocol (legacy) still matches
- db fallback path: RequestPattern.protocol filters
- DeviceModel.to_pattern_db projects the device protocol onto endpoints
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    Command,
    DeviceModel,
    PatternDB,
    PatternMeta,
    ProtocolInfo,
    ServerConfig,
)


class _ProtocolDBManager:
    """DatabaseManager fake returning RequestPattern rows + empty templates."""

    def __init__(self, patterns: list[SimpleNamespace]) -> None:
        self._patterns = patterns

    def device_session(self, device_id: str):  # noqa: ARG002
        session = AsyncMock()

        def _execute(statement, *_args, **_kwargs):  # noqa: ARG003
            # Any statement that resolves to ResponseTemplate returns no row;
            # everything else (the RequestPattern select) returns our rows.
            if "ResponseTemplate" in str(statement):
                return MagicMock(scalar_one_or_none=MagicMock(return_value=None))
            return MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=self._patterns))
                )
            )

        session.execute.side_effect = _execute
        return AsyncMock(__aenter__=AsyncMock(return_value=session))


def _cached_engine(protocol: str):
    """PatternEngine with a single cached endpoint for the given protocol."""
    pattern_db = PatternDB(
        meta=PatternMeta(pattern_id="test", vendor="acme", device_type="ac"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(
                    id="ep1",
                    intent="get_status",
                    method="GET",
                    path="/api/v1/status",
                    protocol=protocol,
                    headers={"required": []},
                ),
            ]
        ),
        server=ServerConfig(responses=[]),
    )
    engine = PatternEngine(_ProtocolDBManager([]))
    engine.apply_pattern_db("device-1", pattern_db)
    return engine


@pytest.mark.asyncio
async def test_cache_same_protocol_matches():
    engine = _cached_engine("http")
    pattern, _, score = await engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}, protocol="http"
    )
    assert pattern is not None
    assert pattern.intent == "get_status"
    assert score > 0.0


@pytest.mark.asyncio
async def test_cache_different_protocol_skipped():
    engine = _cached_engine("http")
    pattern, _, score = await engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}, protocol="mqtt"
    )
    assert pattern is None
    assert score == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_cache_legacy_empty_protocol_never_filters():
    # Endpoint without a protocol (legacy entries) must still match any request.
    engine = _cached_engine("")
    pattern, _, _ = await engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}, protocol="coap"
    )
    assert pattern is not None


@pytest.mark.asyncio
async def test_cache_no_request_protocol_never_filters():
    # A caller that doesn't pass a protocol must see every endpoint.
    engine = _cached_engine("mqtt")
    pattern, _, _ = await engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}
    )
    assert pattern is not None


def _row(protocol: str):
    return SimpleNamespace(
        pattern_id="p1",
        intent="get_status",
        method="GET",
        path_pattern="/api/v1/status",
        required_headers=[],
        query_param_keys=[],
        body_schema=None,
        protocol=protocol,
        match_score=0.9,
    )


@pytest.mark.asyncio
async def test_db_fallback_filters_by_protocol():
    rows = [_row("http"), _row("mqtt")]
    engine = PatternEngine(_ProtocolDBManager(rows))
    # No cache -> falls back to DB; mqtt request must skip the http row.
    pattern, _, _ = await engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}, protocol="mqtt"
    )
    assert pattern is not None
    assert getattr(pattern, "protocol", "") == "mqtt"


@pytest.mark.asyncio
async def test_db_fallback_empty_protocol_never_filters():
    rows = [_row("")]
    engine = PatternEngine(_ProtocolDBManager(rows))
    pattern, _, _ = await engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}, protocol="modbus"
    )
    assert pattern is not None


def test_to_pattern_db_projects_protocol():
    model = DeviceModel(
        meta=PatternMeta(pattern_id="t", vendor="acme", device_type="ac"),
        protocol=ProtocolInfo(protocol="mqtt", identity="acme-device"),
        commands=[
            Command(
                id="c1",
                kind="publish",
                method="PUBLISH",
                topic="sensors/temp",
                path="/sensors/temp",
            )
        ],
    )
    pattern_db = model.to_pattern_db()
    assert pattern_db.client.endpoints[0].protocol == "mqtt"
