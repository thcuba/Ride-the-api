"""C6: un segreto che entra da un import non resta nel buffer né esce dall'API.

La redazione esisteva già (`core/redaction.py`) ma serviva un solo consumatore: il prompt
LLM. Una cattura importata (HAR, PCAP) portava quindi i token che erano sul filo dentro
il buffer del device — il materiale da cui si impara — e `GET /api/devices/{id}/buffer`
li restituiva in chiaro a chiunque chiamasse.
"""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import core.server as server_mod
from core.database import DatabaseManager
from core.pattern_db.buffer_manager import BufferManager
from core.pattern_db.schemas import (
    CaptureDB,
    CaptureDeviceInfo,
    CaptureMeta,
    CaptureSession,
    RawPairWithResponse,
    RawResponse,
)
from core.redaction import REDACTED, redact_buffer_entry, redact_pair
from core.server import app

# Costruiti a runtime: scritti per intero nel file verrebbero redatti dal filtro dei
# segreti del repository, e i test perderebbero di significato.
TOKEN = "Bearer " + "abc123def456ghi789"
PASSWORD = "pw-" + "2verysecret99"
API_KEY = "key-" + "9f8e7d6c5b4a3210"


@pytest_asyncio.fixture
async def dm(tmp_path):
    manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await manager.initialize()
    yield manager
    await manager.close()


def _capture(
    headers: dict | None = None,
    query_params: dict | None = None,
    body: dict | None = None,
) -> CaptureDB:
    """Una cattura con un pair che porta credenziali, come in un HAR vero."""
    now = datetime.now(UTC)
    return CaptureDB(
        meta=CaptureMeta(
            capture_id="c1",
            vendor="shelly",
            device_type="ac",
            capture_date=now,
        ),
        device_info=CaptureDeviceInfo(device_id="payload-device"),
        sessions=[
            CaptureSession(
                session_id="s1",
                timestamp_start=now,
                pairs=[
                    RawPairWithResponse(
                        pair_id="p1",
                        timestamp=now,
                        protocol="http",
                        method="POST",
                        path="/rpc/x",
                        headers=headers if headers is not None else {},
                        query_params=query_params if query_params is not None else {},
                        body=body if body is not None else {},
                        response=RawResponse(status_code=200, body={"ok": True}),
                    )
                ],
            )
        ],
    )


# ---------------------------------------------------------------------------
#  redact_pair
# ---------------------------------------------------------------------------


def test_a_bearer_token_is_redacted_in_the_headers():
    pair = redact_pair({"request_headers": {"Authorization": TOKEN, "Accept": "*/*"}})

    assert pair["request_headers"]["Authorization"] == REDACTED
    assert pair["request_headers"]["Accept"] == "*/*"


def test_a_credential_in_the_query_is_redacted():
    pair = redact_pair({"request_query": {"api_key": API_KEY, "mode": "cool"}})

    assert pair["request_query"]["api_key"] == REDACTED
    assert pair["request_query"]["mode"] == "cool"


def test_a_password_nested_in_the_body_is_redacted():
    pair = redact_pair({"request_body": {"auth": {"password": PASSWORD}, "mode": "cool"}})

    assert pair["request_body"]["auth"]["password"] == REDACTED
    assert pair["request_body"]["mode"] == "cool"


def test_the_original_pair_is_not_mutated():
    """Chi tiene in mano il pair grezzo deve continuare ad averlo grezzo."""
    original = {"request_headers": {"Authorization": TOKEN}}

    redact_pair(original)

    assert original["request_headers"]["Authorization"] == TOKEN


def test_an_innocuous_pair_is_left_alone():
    """Niente over-redazione: i campi che non sono segreti restano leggibili."""
    pair = {
        "method": "POST",
        "path": "/rpc/x",
        "request_headers": {"Content-Type": "application/json"},
        "request_body": {"mode": "cool", "temperature": 22},
    }

    assert redact_pair(pair) == pair


# ---------------------------------------------------------------------------
#  redact_buffer_entry: il pair sta annidato sotto "pair"
# ---------------------------------------------------------------------------


def test_a_buffer_entry_is_redacted_through_its_nested_pair():
    """Redigere le chiavi dell'entry non toccherebbe nulla: il pair è annidato."""
    entry = {
        "pair": {"request_headers": {"Authorization": TOKEN}, "path": "/rpc/x"},
        "size": 42,
    }

    redacted = redact_buffer_entry(entry)

    assert redacted["pair"]["request_headers"]["Authorization"] == REDACTED
    assert redacted["size"] == entry["size"]
    assert entry["pair"]["request_headers"]["Authorization"] == TOKEN


def test_an_entry_without_a_pair_is_returned_unchanged():
    entry = {"size": 1}

    assert redact_buffer_entry(entry) is entry


# ---------------------------------------------------------------------------
#  L'import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_imported_har_puts_only_redacted_values_in_the_buffer(dm, monkeypatch):
    """Il cuore di C6: ciò che entra dal file non porta i segreti nel buffer."""
    stored: list[dict] = []

    async def spy(_self, _device_id, pair, *_args, **_kwargs):
        stored.append(pair)
        return 0

    monkeypatch.setattr(BufferManager, "add_pair", spy)

    capture = _capture(
        headers={"Authorization": TOKEN, "Content-Type": "application/json"},
        query_params={"api_key": API_KEY},
        body={"password": PASSWORD, "mode": "cool"},
    )

    count = await BufferManager(dm).import_capture(capture, target_device_id="dev-imp")

    assert count == 1
    pair = stored[0]
    assert pair["request_headers"]["Authorization"] == REDACTED
    assert pair["request_headers"]["Content-Type"] == "application/json"
    assert pair["request_query"]["api_key"] == REDACTED
    assert pair["request_body"]["password"] == REDACTED
    assert pair["request_body"]["mode"] == "cool"
    dumped = str(pair)
    assert TOKEN not in dumped
    assert PASSWORD not in dumped
    assert API_KEY not in dumped


# ---------------------------------------------------------------------------
#  L'API che elenca il buffer
# ---------------------------------------------------------------------------


class _FakeBuffer:
    @staticmethod
    async def get_buffer_pairs(_device_id) -> list[dict]:
        return [
            {
                "pair": {
                    "pair_id": "p1",
                    "method": "POST",
                    "path": "/rpc/x",
                    "request_headers": {"Authorization": TOKEN},
                    "request_query": {"api_key": API_KEY},
                    "request_body": {"password": PASSWORD},
                    "response_headers": {},
                    "response_body": {"ok": True},
                },
                "size": 64,
            }
        ]


class _FakeBufferMap:
    def get(self, _device_id):
        return _FakeBuffer()


class _FakeOrchestrator:
    buffer = _FakeBufferMap()


@pytest.fixture
def api(dm, monkeypatch):
    monkeypatch.setattr(server_mod, "db_manager", dm)
    monkeypatch.setattr(server_mod, "orchestrator", _FakeOrchestrator())
    cfg = server_mod.config_manager.config
    saved_auth = cfg.security.auth_enabled
    cfg.security.auth_enabled = False
    try:
        yield TestClient(app)
    finally:
        cfg.security.auth_enabled = saved_auth


def test_listing_the_buffer_does_not_expose_credentials(api):
    """L'endpoint restituiva i pair grezzi: token e password in chiaro."""
    resp = api.get("/api/devices/dev-x/buffer")

    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    entry = body["entries"][0]
    assert entry["pair"]["request_headers"]["Authorization"] == REDACTED
    assert entry["pair"]["request_query"]["api_key"] == REDACTED
    assert entry["pair"]["request_body"]["password"] == REDACTED
    # I campi non sensibili restano: l'endpoint serve a capire cosa c'è nel buffer.
    assert entry["pair"]["method"] == "POST"
    assert entry["pair"]["response_body"] == {"ok": True}
    assert entry["size"] == 64  # noqa: PLR2004
    assert TOKEN not in resp.text
    assert PASSWORD not in resp.text
    assert API_KEY not in resp.text
