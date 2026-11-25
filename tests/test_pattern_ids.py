"""Identità dei pattern (C2) e protocollo di default (C3).

Due difetti che, in modi diversi, facevano arrivare al device la risposta sbagliata o
nessuna risposta:

* **C2** — l'id del pattern era ``{device_id}_{kind}_{md5(path)[:8]}``: metodo e
  protocollo non facevano parte dell'identità, quindi ``GET /api/ctl`` e
  ``POST /api/ctl`` producevano lo stesso id e la seconda scrittura sovrascriveva la
  prima (path, body template, status code). Il digest era di 32 bit, e il path viene
  dall'output di un LLM che il traffico del device può influenzare.
* **C3** — il protocollo di un pattern nuovo era il letterale ``"http"``, e il prompt di
  default non chiedeva il campo: per un device MQTT, CoAP o Modbus nessun pattern
  poteva combaciare, perché ``_protocol_matches`` considera una famiglia sola solo
  http/https.
"""

import hashlib
from unittest.mock import AsyncMock

import pytest

from core.pattern_db.ids import (
    DIGEST_CHARS,
    legacy_pattern_id,
    pattern_id,
    resolve_pattern_id,
)
from core.pipeline import LearningPipeline

# ---------------------------------------------------------------------------
#  C2 — identità del pattern
# ---------------------------------------------------------------------------


def test_method_and_protocol_are_part_of_the_identity():
    """GET e POST sullo stesso path non devono collassare in una riga sola."""
    get = pattern_id("dev", "cmd", "GET", "http", "/api/ctl")
    post = pattern_id("dev", "cmd", "POST", "http", "/api/ctl")

    assert get != post


def test_the_protocol_is_part_of_the_identity():
    """Lo stesso comando su due trasporti diversi sono due pattern."""
    http = pattern_id("dev", "cmd", "GET", "http", "/api/ctl")
    mqtt = pattern_id("dev", "cmd", "GET", "mqtt", "/api/ctl")

    assert http != mqtt


def test_the_same_command_gets_a_stable_id():
    """Il case di method e protocollo non deve cambiare l'identità."""
    assert pattern_id("dev", "cmd", "post", "HTTP", "/api/ctl") == pattern_id(
        "dev", "cmd", "POST", "http", "/api/ctl"
    )


def test_the_digest_is_no_longer_32_bits():
    """8 caratteri (32 bit) si possono far collidere: il nuovo id ne usa il doppio."""
    legacy = legacy_pattern_id("dev", "cmd", "/api/ctl").rsplit("_", 1)[-1]
    digest = pattern_id("dev", "cmd", "GET", "http", "/api/ctl").rsplit("_", 1)[-1]

    assert len(digest) == DIGEST_CHARS
    assert len(digest) >= 2 * len(legacy)


def test_the_legacy_formula_is_reproduced_exactly():
    """La formula vecchia serve a ritrovare le righe già su disco."""
    expected = f"dev_cmd_{hashlib.md5(b'/api/ctl').hexdigest()[:8]}"

    assert legacy_pattern_id("dev", "cmd", "/api/ctl") == expected


def test_the_kind_stays_readable_in_the_id():
    """L'id resta riconoscibile a occhio nel DB e nell'export."""
    assert pattern_id("dev", "setpoint", "POST", "http", "/api").startswith("dev_setpoint_")


# ---------------------------------------------------------------------------
#  C2 — riuso delle righe già esistenti (nessun pattern duplicato all'upgrade)
# ---------------------------------------------------------------------------


def test_a_legacy_row_is_reused_for_the_same_command():
    legacy = legacy_pattern_id("dev", "cmd", "/api/ctl")

    resolved = resolve_pattern_id(
        "dev", "cmd", method="GET", protocol="http", path="/api/ctl", existing={legacy: "GET"}
    )

    assert resolved == legacy


def test_a_legacy_row_is_not_reused_for_a_different_method():
    """Il bug originale: il POST riscriveva la riga del GET perché l'id era lo stesso."""
    legacy = legacy_pattern_id("dev", "cmd", "/api/ctl")

    resolved = resolve_pattern_id(
        "dev", "cmd", method="POST", protocol="http", path="/api/ctl", existing={legacy: "GET"}
    )

    assert resolved != legacy
    assert resolved == pattern_id("dev", "cmd", "POST", "http", "/api/ctl")


def test_a_legacy_row_without_a_method_is_not_reused():
    """Senza il metodo non si sa di che comando sia la riga: non si sovrascrive."""
    legacy = legacy_pattern_id("dev", "cmd", "/api/ctl")

    resolved = resolve_pattern_id(
        "dev", "cmd", method="GET", protocol="http", path="/api/ctl", existing={legacy: ""}
    )

    assert resolved != legacy


def test_the_new_id_wins_when_the_row_already_uses_it():
    new = pattern_id("dev", "cmd", "GET", "http", "/api/ctl")
    legacy = legacy_pattern_id("dev", "cmd", "/api/ctl")

    resolved = resolve_pattern_id(
        "dev",
        "cmd",
        method="GET",
        protocol="http",
        path="/api/ctl",
        existing={new: "GET", legacy: "GET"},
    )

    assert resolved == new


def test_without_existing_rows_the_new_id_is_used():
    resolved = resolve_pattern_id("dev", "cmd", method="GET", path="/x", existing={})

    assert resolved == pattern_id("dev", "cmd", "GET", None, "/x")


# ---------------------------------------------------------------------------
#  C3 — il protocollo viene dal device, non da un letterale
# ---------------------------------------------------------------------------


def _pipeline_with_header(header, raises=False) -> LearningPipeline:
    """La pipeline senza __init__: _device_protocol usa solo db_manager."""
    pipeline = object.__new__(LearningPipeline)
    pipeline.db_manager = AsyncMock()
    if raises:
        pipeline.db_manager.read_device_meta = AsyncMock(side_effect=RuntimeError("header"))
    else:
        pipeline.db_manager.read_device_meta = AsyncMock(return_value=header)
    return pipeline


@pytest.mark.asyncio
async def test_the_device_protocol_is_used():
    """Un device MQTT non deve avere pattern nati come "http"."""
    pipeline = _pipeline_with_header({"protocols": ["mqtt"]})

    assert await pipeline._device_protocol("dev-1") == "mqtt"


@pytest.mark.asyncio
async def test_the_protocols_list_wins_over_the_connection_mode():
    pipeline = _pipeline_with_header({"protocols": ["coap"], "connection_mode": "http"})

    assert await pipeline._device_protocol("dev-1") == "coap"


@pytest.mark.asyncio
async def test_a_bare_string_protocol_is_accepted():
    pipeline = _pipeline_with_header({"protocols": "modbus"})

    assert await pipeline._device_protocol("dev-1") == "modbus"


@pytest.mark.asyncio
async def test_the_connection_mode_is_used_when_no_protocol_is_recorded():
    pipeline = _pipeline_with_header({"connection_mode": "mqtt"})

    assert await pipeline._device_protocol("dev-1") == "mqtt"


@pytest.mark.asyncio
async def test_http_remains_the_fallback():
    pipeline = _pipeline_with_header({})

    assert await pipeline._device_protocol("dev-1") == "http"


@pytest.mark.asyncio
async def test_an_unknown_token_falls_back_to_http():
    """Un valore fuori whitelist non deve diventare il protocollo di un pattern."""
    pipeline = _pipeline_with_header({"protocols": ["bogus-proto"]})

    assert await pipeline._device_protocol("dev-1") == "http"


@pytest.mark.asyncio
async def test_a_missing_header_does_not_lose_the_pattern():
    pipeline = _pipeline_with_header(None, raises=True)

    assert await pipeline._device_protocol("dev-1") == "http"
