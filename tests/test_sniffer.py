"""
Tests for Sniffer subsystem (HAR, PCAP/PCAPNG parsing and buffer ingestion).
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from scapy.layers.inet import IP, TCP
from scapy.packet import Raw
from scapy.utils import PcapWriter

import core.server as server_mod
from core.database.manager import DatabaseManager
from core.models.device import DeviceRegistry
from core.pattern_db.schemas import CaptureDB
from core.sniffer.har_parser import parse_har_data
from core.sniffer.pcap_parser import parse_pcap_data
from core.sniffer.service import SnifferService, detect_capture_format

EXPECTED_PAIRS_COUNT = 2
STATUS_OK = 200
STATUS_UNPROCESSABLE = 422


@pytest.fixture
def sample_har_dict() -> dict:
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "TestHar", "version": "1.0"},
            "entries": [
                {
                    "startedDateTime": "2026-11-21T15:30:00.000Z",
                    "time": 42.5,
                    "request": {
                        "method": "POST",
                        "url": "https://api.iot.example.com/v1/devices/status?query_key=123",
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {"name": "Content-Type", "value": "application/json"},
                            {"name": "Authorization", "value": "Bearer token123"},
                        ],
                        "queryString": [{"name": "query_key", "value": "123"}],
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"action": "power_on", "target": "relay_1"}',
                        },
                    },
                    "response": {
                        "status": 200,
                        "statusText": "OK",
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "content": {
                            "mimeType": "application/json",
                            "text": '{"success": true, "state": "on"}',
                        },
                    },
                },
                {
                    "startedDateTime": "2026-11-21T15:30:05.000Z",
                    "time": 15.0,
                    "request": {
                        "method": "GET",
                        "url": "https://api.iot.example.com/v1/devices/metrics",
                        "httpVersion": "HTTP/1.1",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "statusText": "OK",
                        "headers": [],
                        "content": {
                            "mimeType": "text/plain",
                            "text": "temperature=21.5",
                        },
                    },
                },
            ],
        }
    }


def test_detect_capture_format() -> None:
    har_bytes = b'{"log": {"version": "1.2", "entries": []}}'
    assert detect_capture_format(har_bytes) == "har"
    assert detect_capture_format(b"", "capture.har") == "har"

    pcap_le = b"\xd4\xc3\xb2\xa1\x00\x00..."
    assert detect_capture_format(pcap_le) == "pcap"

    pcapng = b"\x0a\x0d\x0d\x0a\x00\x00..."
    assert detect_capture_format(pcapng) == "pcapng"

    ride_cap = b'{"$schema": "https://ride-the-api.dev/capture-schema/v1", "meta": {}}'
    assert detect_capture_format(ride_cap) == "ride_capture"


def test_har_parser(sample_har_dict: dict) -> None:
    capture = parse_har_data(
        sample_har_dict,
        vendor="tuya",
        device_type="switch",
        device_id="dev-switch-01",
    )
    assert isinstance(capture, CaptureDB)
    assert capture.meta.vendor == "tuya"
    assert len(capture.sessions) == 1
    session = capture.sessions[0]
    assert len(session.pairs) == EXPECTED_PAIRS_COUNT

    p1 = session.pairs[0]
    assert p1.method == "POST"
    assert p1.path == "/v1/devices/status"
    assert p1.query_params == {"query_key": "123"}
    assert p1.body == {"action": "power_on", "target": "relay_1"}
    assert p1.response is not None
    assert p1.response.status_code == STATUS_OK
    assert p1.response.body == {"success": True, "state": "on"}

    p2 = session.pairs[1]
    assert p2.method == "GET"
    assert p2.path == "/v1/devices/metrics"
    assert p2.response is not None
    assert p2.response.body == "temperature=21.5"


def test_pcap_parser_synthetic() -> None:
    req_data = (
        b"POST /api/v1/control HTTP/1.1\r\n"
        b"Host: device.lan\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 16\r\n\r\n"
        b'{"command":"set"}'
    )
    resp_data = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 17\r\n\r\n"
        b'{"result":"done"}'
    )

    pkt_req = (
        IP(src="192.168.1.50", dst="192.168.1.1")
        / TCP(sport=54321, dport=80, seq=1000)
        / Raw(load=req_data)
    )
    pkt_resp = (
        IP(src="192.168.1.1", dst="192.168.1.50")
        / TCP(sport=80, dport=54321, seq=5000)
        / Raw(load=resp_data)
    )

    buf = io.BytesIO()
    writer = PcapWriter(buf)
    writer.write([pkt_req, pkt_resp])
    writer.flush()
    pcap_bytes = buf.getvalue()

    capture = parse_pcap_data(
        pcap_bytes,
        vendor="custom",
        device_type="sensor",
        device_id="dev-sensor",
    )
    assert len(capture.sessions) == 1
    pairs = capture.sessions[0].pairs
    assert len(pairs) == 1

    p = pairs[0]
    assert p.method == "POST"
    assert p.path == "/api/v1/control"
    assert p.body == {"command": "set"}
    assert p.response is not None
    assert p.response.status_code == STATUS_OK
    assert p.response.body == {"result": "done"}


@pytest.mark.asyncio
async def test_sniffer_service_and_api(
    tmp_path: Path, sample_har_dict: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)

    db_path = tmp_path / "test_sniffer_core.db"
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir(parents=True, exist_ok=True)
    db_manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{db_path}",
        device_db_dir=devices_dir,
    )
    await db_manager.initialize()

    device_id = "test-device-sniffer"
    async with db_manager.core_session() as session:
        dev = DeviceRegistry(
            device_id=device_id,
            name="Test Sniffer Device",
            vendor="example_vendor",
            device_type="light",
        )
        session.add(dev)
        await session.commit()

    service = SnifferService(db_manager)
    har_bytes = json.dumps(sample_har_dict).encode("utf-8")

    res = await service.import_to_device(
        device_id=device_id,
        content=har_bytes,
        filename="test.har",
    )
    assert res["status"] == "success"
    assert res["imported"] == EXPECTED_PAIRS_COUNT
    assert res["format"] == "har"

    server_mod.db_manager = db_manager

    transport = ASGITransport(app=server_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("traffic.har", har_bytes, "application/json")}
        resp = await client.post(f"/api/devices/{device_id}/sniffer/import", files=files)
        assert resp.status_code == STATUS_OK
        data = resp.json()
        assert data["status"] == "success"
        assert data["imported"] == EXPECTED_PAIRS_COUNT

    await db_manager.close()


@pytest.mark.asyncio
async def test_sniffer_import_parse_error_does_not_leak_internals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable capture must return a generic 422 without internal details."""
    monkeypatch.setattr(server_mod.config_manager.config.security, "auth_enabled", False)

    db_path = tmp_path / "test_sniffer_leak.db"
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir(parents=True, exist_ok=True)
    db_manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{db_path}",
        device_db_dir=devices_dir,
    )
    await db_manager.initialize()
    server_mod.db_manager = db_manager

    transport = ASGITransport(app=server_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {
            "file": ("internalsecret.bin", b"\x00\x01\x02not-a-capture", "application/octet-stream")
        }
        resp = await client.post("/api/devices/dev-leak/sniffer/import", files=files)
        assert resp.status_code == STATUS_UNPROCESSABLE
        data = resp.json()
        assert data["error"].startswith("Unable to parse capture file")
        # Nessun dettaglio interno (nome file, traccia, eccezione) deve trapelare.
        assert "internalsecret" not in json.dumps(data)

    await db_manager.close()
