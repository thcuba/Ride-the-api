"""Building a local response must not corrupt the template or mangle values (C-10)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.pattern_db.pattern_engine import PatternEngine
from core.pattern_db.schemas import FieldMapping, ServerResponse


@pytest.fixture
def engine():
    return PatternEngine(MagicMock())


def _template(body, mappings=()):
    return ServerResponse(id="t", triggers=["x"], body_template=body, field_mappings=list(mappings))


@pytest.mark.asyncio
async def test_template_is_not_mutated_across_requests(engine):
    template = _template(
        {"data": {"temp": 20}},
        [FieldMapping(source="request.body.temp", target="data.temp")],
    )
    first = await engine.build_local_response("d", template, {"body": {"temp": 25}})
    assert first["body"]["data"]["temp"] == 25  # noqa: PLR2004
    # The cached template keeps its own value...
    assert template.body_template == {"data": {"temp": 20}}
    # ...so a request without the field gets the template value, not the last one.
    second = await engine.build_local_response("d", template, {"body": {}})
    assert second["body"]["data"]["temp"] == 20  # noqa: PLR2004


@pytest.mark.asyncio
async def test_whole_string_placeholders_keep_their_type(engine):
    store = engine.get_state_store("d")
    store.set("power", 0)
    store.set("on", False)
    store.set("t", 21.5)
    template = _template(
        {
            "power": "{state.power}",
            "on": "{state.on}",
            "t": "{state.t}",
            "missing": "{state.nope}",
            "msg": "t={state.t} on={state.on} power={state.power}",
        }
    )
    out = (await engine.build_local_response("d", template, {}))["body"]
    assert out["power"] == 0
    assert out["on"] is False
    assert out["t"] == 21.5  # noqa: PLR2004
    assert out["missing"] == ""
    assert out["msg"] == "t=21.5 on=false power=0"


@pytest.mark.asyncio
async def test_request_zero_is_not_rendered_empty(engine):
    template = _template({"level": "{request.body.level}", "txt": "L{request.body.level}"})
    out = (await engine.build_local_response("d", template, {"body": {"level": 0}}))["body"]
    assert out["level"] == 0
    assert out["txt"] == "L0"


@pytest.mark.asyncio
async def test_bracket_target_writes_into_list(engine):
    template = _template(
        {"items": [{"v": 1}, {"v": 2}]},
        [FieldMapping(source="request.body.x", target="items[1].v")],
    )
    out = (await engine.build_local_response("d", template, {"body": {"x": 7}}))["body"]
    assert out["items"] == [{"v": 1}, {"v": 7}]


@pytest.mark.asyncio
async def test_unwritable_target_is_skipped_not_fatal(engine):
    template = _template(
        {"items": [], "ok": True},
        [
            FieldMapping(source="request.body.x", target="items[5].v"),  # past the end
            FieldMapping(source="request.body.x", target="ok.deeper"),  # scalar in the way
        ],
    )
    out = (await engine.build_local_response("d", template, {"body": {"x": 7}}))["body"]
    assert out == {"items": [], "ok": True}
