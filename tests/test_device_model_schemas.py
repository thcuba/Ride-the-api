"""Tests for the v2 DeviceModel schemas (additive).

Covers the new portable model introduced for the DeviceModel architecture:
- Observation (a generic uni-directional traffic event, not a pair)
- ProtocolInfo (first-flush auto identification)
- DeviceModel (root portable record)
- the v2 JSON Schema validation
- backward compatibility of the v1 PatternDB models
"""

from __future__ import annotations

from datetime import UTC, datetime

import jsonschema

from core.pattern_db.schemas import (
    Command,
    DeviceModel,
    Observation,
    ObservationKind,
    PatternDB,
    PatternMeta,
    ProtocolInfo,
    StateVariable,
    TransportMeta,
    VirtualSensor,
)
from core.pattern_db.validator import get_pattern_schema_v2, validate_pattern


def _now() -> datetime:
    return datetime.now(UTC)


def test_observation_is_uni_directional() -> None:
    """An MQTT publish has no request/response pair — it must be representable."""
    obs = Observation(
        id="obs1",
        device_id="mqtt-shelly1",
        timestamp=_now(),
        protocol="mqtt",
        kind=ObservationKind.PUBLISH,
        transport=TransportMeta(topic="shellies/shelly1/relay/0", qos=1, retain=False),
        content={"on": True},
    )
    assert obs.kind == "publish"
    assert obs.protocol == "mqtt"
    assert obs.transport.topic == "shellies/shelly1/relay/0"
    assert obs.in_reply_to is None  # no forcing into a pair


def test_observation_kind_defaults_to_request() -> None:
    obs = Observation(id="obs2", device_id="dev-1", timestamp=_now())
    assert obs.kind == ObservationKind.REQUEST
    assert obs.transport.port == 0


def test_protocol_info_auto_identification() -> None:
    confidence = 0.92
    info = ProtocolInfo(
        transport="tcp",
        protocol="mqtt",
        proprietary=False,
        security="none",
        handler="mqtt",
        ports=[1883],
        confidence=confidence,
    )
    assert info.protocol == "mqtt"
    assert info.proprietary is False
    assert info.handler == "mqtt"
    assert info.confidence == confidence


def test_device_model_roundtrip_carries_learned_knowledge() -> None:
    dm = DeviceModel(
        meta=PatternMeta(
            pattern_id="ip-1-2-3-4-patterns", vendor="shelly", device_type="plug"
        ),
        protocol=ProtocolInfo(protocol="mqtt", handler="mqtt", confidence=0.9),
        commands=[
            Command(
                id="c1",
                kind="set_relay",
                protocol="mqtt",
                topic="shellies/shelly1/relay/0",
            )
        ],
        state_variables=[StateVariable(name="power", type="float", unit="W")],
        virtual_sensors=[VirtualSensor(name="power_reading", behavior="drift")],
    )
    dump = dm.model_dump(by_alias=True, exclude_none=True)
    assert dump["$schema"] == "https://ride-the-api.dev/pattern-schema/v2"
    assert dump["commands"][0]["kind"] == "set_relay"
    assert dump["state_variables"][0]["name"] == "power"


def test_device_model_validates_against_v2_schema() -> None:
    dm = DeviceModel(
        meta=PatternMeta(pattern_id="p-patterns", vendor="v", device_type="plug"),
        commands=[Command(id="c1", kind="turn_on")],
    )
    jsonschema.validate(dm.model_dump(by_alias=True, exclude_none=True), get_pattern_schema_v2())


def test_v1_pattern_db_still_parses() -> None:
    """Backward compatibility: the v1 PatternDB root is untouched by v2 addition."""
    pdb = PatternDB(
        meta=PatternMeta(pattern_id="x-patterns", vendor="v", device_type="d")
    )
    assert pdb.meta.pattern_id == "x-patterns"


def test_v1_pattern_db_validates_against_v1_schema() -> None:
    """v1 data still passes the v1 JSON schema validation helper."""
    pdb = PatternDB(
        meta=PatternMeta(pattern_id="x-patterns", vendor="v", device_type="d")
    )
    result = validate_pattern(pdb.model_dump(by_alias=True, exclude_none=True))
    assert result.valid
