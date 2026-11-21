"""
Sniffer Service for Ride-the-API.

Coordinates format detection, parsing, and buffer ingestion of external
traffic captures (HAR, PCAP, PCAPNG, and native .ride-capture.json).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from core.models.device import DeviceRegistry
from core.pattern_db import buffer_manager
from core.pattern_db.schemas import CaptureDB
from core.sniffer.har_parser import parse_har_data
from core.sniffer.pcap_parser import parse_pcap_data

logger = logging.getLogger(__name__)

PCAP_MAGIC_LE = b"\xd4\xc3\xb2\xa1"
PCAP_MAGIC_BE = b"\xa1\xb2\xc3\xd4"
PCAP_MAGIC_NANO_LE = b"\x4d\x3c\xb2\xa1"
PCAP_MAGIC_NANO_BE = b"\xa1\xb2\x3c\x4d"
PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"


def _detect_by_filename(filename: str) -> str | None:
    lower_fn = filename.lower()
    if lower_fn.endswith(".pcapng"):
        return "pcapng"
    if lower_fn.endswith(".pcap"):
        return "pcap"
    if lower_fn.endswith(".har"):
        return "har"
    return None


def _detect_json_format(content: bytes) -> str | None:
    stripped = content.strip()
    if not stripped.startswith(b"{"):
        return None
    sample = stripped[:4096].decode("utf-8", errors="ignore")
    if '"log"' in sample and '"entries"' in sample:
        return "har"
    if '"schema_url"' in sample or '"$schema"' in sample or '"sessions"' in sample:
        return "ride_capture"
    try:
        parsed = json.loads(content.decode("utf-8", errors="replace"))
        if isinstance(parsed, dict):
            if "log" in parsed:
                return "har"
            if "sessions" in parsed or "$schema" in parsed:
                return "ride_capture"
    except Exception:
        pass
    return None


def detect_capture_format(content: bytes, filename: str | None = None) -> str:
    """Detect whether capture data is PCAP, PCAPNG, HAR, or native .ride-capture.json."""
    if filename:
        fn_format = _detect_by_filename(filename)
        if fn_format:
            return fn_format

    if not content:
        return "unknown"

    if content.startswith(PCAPNG_MAGIC):
        return "pcapng"
    if content.startswith((PCAP_MAGIC_LE, PCAP_MAGIC_BE, PCAP_MAGIC_NANO_LE, PCAP_MAGIC_NANO_BE)):
        return "pcap"

    json_format = _detect_json_format(content)
    if json_format:
        return json_format

    return "unknown"


class SnifferService:
    """Service to process and import sniffer captures into device buffers."""

    def __init__(self, db_manager: Any = None) -> None:  # noqa: ANN401
        self.db_manager = db_manager

    def parse_capture(
        self,
        content: bytes,
        filename: str | None = None,
        vendor: str = "sniffer",
        device_type: str = "iot",
        device_id: str = "obfuscated",
    ) -> tuple[CaptureDB, str]:
        """Parse raw capture bytes into a CaptureDB and return (capture, format_detected)."""
        fmt = detect_capture_format(content, filename)

        if fmt == "har":
            logger.info("Parsing HAR capture (size: %d bytes)", len(content))
            return parse_har_data(
                content, vendor=vendor, device_type=device_type, device_id=device_id
            ), fmt

        if fmt in ("pcap", "pcapng"):
            logger.info("Parsing %s capture (size: %d bytes)", fmt.upper(), len(content))
            return parse_pcap_data(
                content, vendor=vendor, device_type=device_type, device_id=device_id
            ), fmt

        if fmt == "ride_capture":
            logger.info("Parsing native .ride-capture.json (size: %d bytes)", len(content))
            return CaptureDB.model_validate_json(content), fmt

        try:
            return parse_har_data(
                content, vendor=vendor, device_type=device_type, device_id=device_id
            ), "har"
        except Exception:
            pass

        try:
            return parse_pcap_data(
                content, vendor=vendor, device_type=device_type, device_id=device_id
            ), "pcap"
        except Exception:
            pass

        msg = (
            f"Unrecognized capture format for file {filename or 'payload'}. "
            "Expected .har, .pcap, .pcapng, or .ride-capture.json"
        )
        raise ValueError(msg)

    async def import_to_device(
        self,
        device_id: str,
        content: bytes,
        filename: str | None = None,
        vendor: str | None = None,
    ) -> dict[str, Any]:
        """Parse capture data and import directly into the device's buffer."""
        if not self.db_manager:
            msg = "Database manager is not initialized"
            raise RuntimeError(msg)

        dev_vendor = vendor or "sniffer"
        dev_type = "iot"
        try:
            async with self.db_manager.core_session() as session:
                res = await session.execute(
                    select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
                )
                dev = res.scalar_one_or_none()
                if dev:
                    dev_vendor = dev.vendor or dev_vendor
                    dev_type = dev.device_type or dev_type
        except Exception as e:
            logger.debug("Device lookup for %s skipped or failed: %s", device_id, e)

        capture, detected_fmt = self.parse_capture(
            content=content,
            filename=filename,
            vendor=dev_vendor,
            device_type=dev_type,
            device_id=device_id,
        )

        manager = buffer_manager.BufferManager(self.db_manager)
        count = await manager.import_capture(capture, target_device_id=device_id)

        pairs_summary = []
        for session in capture.sessions:
            for p in session.pairs:
                pairs_summary.append(
                    {
                        "method": p.method,
                        "path": p.path,
                        "status": p.response.status_code if p.response else None,
                        "timestamp": p.timestamp.isoformat(),
                    }
                )

        return {
            "status": "success",
            "imported": count,
            "device_id": device_id,
            "format": detected_fmt,
            "filename": filename,
            "pairs_count": len(pairs_summary),
            "pairs_summary": pairs_summary[:20],
        }
