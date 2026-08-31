"""
Tests for the pattern engine -- matching, similarity, response building, file I/O.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.pattern_db.pattern_engine import PatternEngine, _normalize_field_mappings
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    FieldMapping,
    PatternDB,
    PatternMeta,
    ServerConfig,
    ServerResponse,
    StateVariable,
    VirtualSensor,
)
from core.pattern_db.state_manager import DeviceStateStore


class MockDBManager:
    """Simulates DatabaseManager for testing pattern engine."""

    def __init__(self) -> None:
        self._session = MagicMock()

    def device_session(self, device_id: str):  # noqa: ARG002
        return AsyncMock(
            __aenter__=AsyncMock(
                return_value=MagicMock(
                    execute=AsyncMock(
                        return_value=MagicMock(
                            scalars=MagicMock(
                                return_value=MagicMock(all=MagicMock(return_value=[]))
                            )
                        )
                    )
                )
            )
        )


@pytest.fixture
def engine():
    """Create a PatternEngine with a mock DB manager."""
    return PatternEngine(MockDBManager())


# Test: state management
def test_get_state_store(engine):
    store = engine.get_state_store("device-1")
    assert isinstance(store, DeviceStateStore)
    assert store.device_id == "device-1"
    store2 = engine.get_state_store("device-1")
    assert store2 is store
    store3 = engine.get_state_store("device-2")
    assert store3 is not store
    assert store3.device_id == "device-2"


def test_apply_pattern_db(engine):
    pattern_db = PatternDB(
        meta=PatternMeta(pattern_id="test-pattern", vendor="acme", device_type="ac"),
        server=ServerConfig(
            state_variables=[StateVariable(name="power", type="boolean", default=False)],
            virtual_sensors=[
                VirtualSensor(name="temperature", type="float", baseline="25.0", behavior="static")
            ],
        ),
    )
    engine.apply_pattern_db("device-1", pattern_db)
    store = engine.get_state_store("device-1")
    assert store.get("power") is False
    assert engine._cached_patterns["device-1"] is pattern_db


# Test: similarity scoring
def test_calculate_similarity_full_match(engine):
    score = engine._calculate_similarity(
        "GET",
        "GET",
        "/api/v1/status",
        "/api/v1/status",
        ["Authorization"],
        {"Authorization": "Bearer xxx"},
        {"type": "object", "properties": {"key": {"type": "string"}}},
        {"key": "value"},
        ["page"],
        {"page": "1"},
    )
    assert score == pytest.approx(1.0)


def test_calculate_similarity_path_with_params(engine):
    score = engine._calculate_similarity(
        "POST",
        "POST",
        "/api/v1/devices/{id}/command",
        "/api/v1/devices/abc123/command",
        [],
        {},
        None,
        None,
        [],
        {},
    )
    assert score == pytest.approx(0.75)


def test_calculate_similarity_method_mismatch(engine):
    score = engine._calculate_similarity(
        "GET",
        "POST",
        "/status",
        "/status",
        [],
        {},
        None,
        None,
        [],
        {},
    )
    assert score == pytest.approx(0.45)


def test_calculate_similarity_path_length_mismatch(engine):
    score = engine._calculate_similarity(
        "GET",
        "GET",
        "/a/b/c",
        "/a/b/c/d/e",
        [],
        {},
        None,
        None,
        [],
        {},
    )
    assert score == pytest.approx(0.45)


def test_calculate_similarity_path_close_mismatch(engine):
    score = engine._calculate_similarity(
        "GET",
        "GET",
        "/a/b/c",
        "/a/b/c/d",
        [],
        {},
        None,
        None,
        [],
        {},
    )
    # total=100, score=30(method)+9(path)+15(body)=54
    assert score == pytest.approx(0.54)


def test_calculate_similarity_no_body_both_empty(engine):
    score = engine._calculate_similarity(
        "GET",
        "GET",
        "/",
        "/",
        [],
        {},
        {},
        {},
        [],
        {},
    )
    # total=100, score=30+30+15=75
    assert score == pytest.approx(0.75)


def test_calculate_similarity_body_partial_match(engine):
    score = engine._calculate_similarity(
        "POST",
        "POST",
        "/",
        "/",
        [],
        {},
        {"properties": {"a": {}, "b": {}, "c": {}}},
        {"a": 1, "b": 2},
        [],
        {},
    )
    # total=100, score=30+30+15*(2/3)=70
    assert score == pytest.approx(0.7)


def test_body_similarity_schema_keys(engine):
    r = engine._body_similarity({"properties": {"a": {}, "b": {}}}, {"a": 1, "b": 2})
    assert r == pytest.approx(1.0)
    r = engine._body_similarity({"properties": {"a": {}, "b": {}, "c": {}}}, {"a": 1})
    assert r == pytest.approx(1 / 3)


def test_body_similarity_no_schema_no_body(engine):
    assert engine._body_similarity({}, {}) == 0.5  # noqa: PLR2004


def test_body_similarity_no_body(engine):
    assert engine._body_similarity({"properties": {"a": {}}}, None) == 0.5  # noqa: PLR2004


def test_path_similarity_exact(engine):
    assert engine._path_similarity("/api/v1/status", "/api/v1/status") == 1.0


def test_path_similarity_with_params(engine):
    assert engine._path_similarity("/api/{id}/status", "/api/abc/status") == 1.0


def test_path_similarity_different_length(engine):
    assert engine._path_similarity("/a/b", "/a/b/c/d") == 0.0


def test_path_similarity_close_length(engine):
    assert engine._path_similarity("/a/b/c", "/a/b/c/d") == pytest.approx(0.3)


# Test: pattern matching with cached pattern DB
@pytest.mark.asyncio
async def test_find_best_match_cached(engine):
    pattern_db = PatternDB(
        meta=PatternMeta(pattern_id="test", vendor="acme", device_type="ac"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(
                    id="ep1",
                    intent="get_status",
                    method="GET",
                    path="/api/v1/status",
                    headers={"required": ["Authorization"]},
                ),
            ]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(
                    id="resp1",
                    triggers=["get_status"],
                    status_code=200,
                    body_template={"status": "ok"},
                ),
            ]
        ),
    )
    engine.apply_pattern_db("device-1", pattern_db)
    pattern, template, score = await engine.find_best_match(
        "device-1",
        "GET",
        "/api/v1/status",
        {"Authorization": "Bearer xxx"},
        None,
        {},
    )
    assert pattern is not None
    assert pattern.intent == "get_status"
    assert template is not None
    assert template.id == "resp1"
    # total=100, score=30+30+15+15=90
    assert score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_find_best_match_no_patterns(engine):
    pattern, template, score = await engine.find_best_match(
        "device-1",
        "GET",
        "/unknown",
        {},
        None,
        {},
    )
    assert pattern is None
    assert template is None
    assert score == pytest.approx(0.0)


# Test: response building
@pytest.mark.asyncio
async def test_build_local_response(engine):
    pattern_db = PatternDB(
        meta=PatternMeta(pattern_id="test", vendor="acme", device_type="ac"),
        server=ServerConfig(
            state_variables=[StateVariable(name="power", type="boolean", default=True)],
        ),
    )
    engine.apply_pattern_db("device-1", pattern_db)
    template = ServerResponse(
        id="resp1",
        triggers=["get_status"],
        status_code=200,
        body_template={"status": "{state.power}"},
    )
    result = await engine.build_local_response(
        "device-1",
        template,
        {"body": {"command": "on"}},
    )
    assert result["status_code"] == 200  # noqa: PLR2004
    assert result["body"]["status"] == "True"


@pytest.mark.asyncio
async def test_build_local_response_with_field_mappings(engine):
    template = ServerResponse(
        id="resp1",
        triggers=["set_temp"],
        status_code=200,
        body_template={"result": None},
        field_mappings=[
            FieldMapping(
                source="request.body.target_temp",
                target="result.temperature",
                transform="direct",
            ),
        ],
    )
    result = await engine.build_local_response(
        "device-1",
        template,
        {"body": {"target_temp": 22.5}},
    )
    assert result["status_code"] == 200  # noqa: PLR2004
    assert result["body"]["result"]["temperature"] == 22.5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_build_local_response_enum_transform(engine):
    template = ServerResponse(
        id="resp1",
        triggers=["set_mode"],
        status_code=200,
        body_template={"mode": None},
        field_mappings=[
            FieldMapping(
                source="request.body.mode",
                target="mode",
                transform="enum",
                mapping={"1": "cool", "2": "heat", "3": "fan"},
            ),
        ],
    )
    result = await engine.build_local_response(
        "device-1",
        template,
        {"body": {"mode": "2"}},
    )
    assert result["body"]["mode"] == "heat"


# Test: template variable resolution
def test_resolve_template_vars_state(engine):
    engine.get_state_store("device-1").set("power", True)
    result = engine._resolve_template_vars(
        "{state.power}",
        engine.get_state_store("device-1"),
        {},
    )
    assert result == "True"


def test_resolve_template_vars_request(engine):
    result = engine._resolve_template_vars(
        "{request.body.temperature}",
        engine.get_state_store("device-1"),
        {"body": {"temperature": 25.5}},
    )
    assert result == "25.5"


def test_resolve_template_vars_uuid(engine):
    r1 = engine._resolve_template_vars("{uuid}", engine.get_state_store("device-1"), {})
    r2 = engine._resolve_template_vars("{uuid}", engine.get_state_store("device-1"), {})
    assert r1 != r2
    assert len(r1) == 36  # noqa: PLR2004


def test_resolve_template_vars_nested_dict(engine):
    engine.get_state_store("device-1").set("name", "Sensor-1")
    template = {"device": {"name": "{state.name}", "value": 42}}
    result = engine._resolve_template_vars(template, engine.get_state_store("device-1"), {})
    assert result["device"]["name"] == "Sensor-1"
    assert result["device"]["value"] == 42  # noqa: PLR2004


def test_resolve_template_vars_nested_list(engine):
    engine.get_state_store("device-1").set("status", "ok")
    template = [{"id": 1, "status": "{state.status}"}]
    result = engine._resolve_template_vars(template, engine.get_state_store("device-1"), {})
    assert result[0]["status"] == "ok"


# Test: formula evaluation
def test_eval_formula_simple(engine):
    result = engine._eval_formula("{state.power} + 1", {}, engine.get_state_store("device-1"))
    assert isinstance(result, (int, float))


def test_eval_formula_with_random(engine):
    result = engine._eval_formula("random(10, 20)", {}, engine.get_state_store("device-1"))
    assert 10 <= result <= 20  # noqa: PLR2004


def test_eval_formula_invalid(engine):
    result = engine._eval_formula("1 / {state.undefined}", {}, engine.get_state_store("device-1"))
    assert result == 0


# Test: pattern DB file I/O
def test_load_pattern_file_not_found(engine):
    with pytest.raises(FileNotFoundError):
        engine.load_pattern_file("device-1", "/nonexistent/path.json")


def test_save_and_load_pattern_file(engine):
    pattern_db = PatternDB(
        meta=PatternMeta(pattern_id="test-save", vendor="acme", device_type="thermostat"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(id="ep1", intent="get_status", method="GET", path="/status"),
            ]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(
                    id="resp1",
                    triggers=["get_status"],
                    status_code=200,
                    body_template={"status": "ok"},
                ),
            ]
        ),
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        fpath = f.name
    try:
        engine.save_pattern_file(pattern_db, fpath)
        loaded = engine.load_pattern_file("device-2", fpath)
        assert loaded.meta.pattern_id == "test-save"
        assert loaded.meta.vendor == "acme"
        assert len(loaded.client.endpoints) == 1
        assert loaded.client.endpoints[0].intent == "get_status"
        store = engine.get_state_store("device-2")
        assert store is not None
    finally:
        Path(fpath).unlink(missing_ok=True)


# Test: JSON path resolution
def test_resolve_json_path_simple(engine):
    assert engine._resolve_json_path({"value": 42}, "value") == 42  # noqa: PLR2004


def test_resolve_json_path_nested(engine):
    assert engine._resolve_json_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1


def test_resolve_json_path_array_index(engine):
    assert engine._resolve_json_path({"items": [10, 20, 30]}, "items[1]") == 20  # noqa: PLR2004


def test_resolve_json_path_dot_in_array(engine):
    assert engine._resolve_json_path({"data": [{"name": "test"}]}, "data.0.name") == "test"


def test_resolve_source_request(engine):
    result = engine._resolve_source(
        "request.body.temperature",
        {"body": {"temperature": 25.5}},
        engine.get_state_store("device-1"),
    )
    assert result == 25.5  # noqa: PLR2004


def test_resolve_source_state(engine):
    engine.get_state_store("device-1").set("power", True)
    result = engine._resolve_source(
        "state.power",
        {},
        engine.get_state_store("device-1"),
    )
    assert result is True


def test_resolve_source_constant(engine):
    result = engine._resolve_source(
        "constant.some_value",
        {},
        engine.get_state_store("device-1"),
    )
    assert result == "some_value"


def test_resolve_source_unknown(engine):
    result = engine._resolve_source(
        "unknown.foo",
        {},
        engine.get_state_store("device-1"),
    )
    assert result is None


def test_set_nested(engine):
    d = {}
    engine._set_nested(d, "a.b.c", 42)
    assert d["a"]["b"]["c"] == 42  # noqa: PLR2004


def test_set_nested_overwrite(engine):
    d = {"x": 1}
    engine._set_nested(d, "x", 2)
    assert d["x"] == 2  # noqa: PLR2004


# Test: field_mappings normalisation (dict from DB ResponseTemplate vs list from cache)
def test_normalize_field_mappings_dict():
    rows = _normalize_field_mappings(
        {"request.body.target_temp": "result.temperature", "state.power": "power"}
    )
    assert len(rows) == 2  # noqa: PLR2004
    by_source = {r["source"]: r for r in rows}
    assert by_source["request.body.target_temp"]["target"] == "result.temperature"
    assert by_source["request.body.target_temp"]["transform"] == "direct"
    assert by_source["state.power"]["target"] == "power"


def test_normalize_field_mappings_empty():
    assert _normalize_field_mappings({}) == []
    assert _normalize_field_mappings(None) == []


@pytest.mark.asyncio
async def test_build_local_response_db_dict_field_mappings(engine):
    """Regression: a DB ResponseTemplate exposes field_mappings as a dict, which
    used to crash build_local_response with AttributeError on .get()."""
    db_template = SimpleNamespace(
        body_template={"result": None},
        status_code=200,
        headers_template={"Content-Type": "application/json"},
        field_mappings={"request.body.target_temp": "result.temperature"},
    )
    result = await engine.build_local_response(
        "device-1",
        db_template,
        {"body": {"target_temp": 22.5}},
    )
    assert result["status_code"] == 200  # noqa: PLR2004
    assert result["body"]["result"]["temperature"] == 22.5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_find_best_match_cached_skips_db():
    """Regression: when a cached pattern file exists, find_best_match must not
    scan the device DB (per-request redundancy)."""
    db = MagicMock()
    db.device_session = MagicMock(side_effect=AssertionError("DB session should not be used"))
    c_engine = PatternEngine(db)

    pattern_db = PatternDB(
        meta=PatternMeta(pattern_id="test", vendor="acme", device_type="ac"),
        client=ClientConfig(
            endpoints=[
                ClientEndpoint(
                    id="ep1",
                    intent="get_status",
                    method="GET",
                    path="/api/v1/status",
                ),
            ]
        ),
        server=ServerConfig(
            responses=[
                ServerResponse(
                    id="resp1",
                    triggers=["get_status"],
                    status_code=200,
                    body_template={"status": "ok"},
                ),
            ]
        ),
    )
    c_engine.apply_pattern_db("device-1", pattern_db)
    pattern, template, _ = await c_engine.find_best_match(
        "device-1", "GET", "/api/v1/status", {}, None, {}
    )
    assert pattern is not None
    assert template is not None
    db.device_session.assert_not_called()
