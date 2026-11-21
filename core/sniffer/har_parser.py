"""
HAR (HTTP Archive 1.2) Parser for Ride-the-API.

Parses standard HAR captures exported from intercepting proxies and sniffers
(mitmproxy, Charles, Proxyman, Fiddler, DevTools, HTTP Toolkit) into
portable .ride-capture.json CaptureDB structures ready for the LLM buffer.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import json_repair

from core.pattern_db.schemas import (
    CaptureDB,
    CaptureDeviceInfo,
    CaptureMeta,
    CaptureSession,
    RawPairWithResponse,
    RawResponse,
)

logger = logging.getLogger(__name__)


def _parse_body(text: str | None, mime_type: str | None, encoding: str | None = None) -> Any:  # noqa: ANN401
    """Extract and parse HTTP body text/json or base64 into an appropriate object."""
    if not text:
        return None

    decoded_text = text
    if encoding == "base64":
        try:
            raw_bytes = base64.b64decode(text)
            try:
                decoded_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return base64.b64encode(raw_bytes).decode("ascii")
        except Exception:
            decoded_text = text

    # Try parsing as JSON if mimeType suggests it or text starts with { / [
    stripped = decoded_text.strip()
    is_json_mime = mime_type and "json" in mime_type.lower()
    is_json_chars = stripped.startswith(("{", "[")) and stripped.endswith(("}", "]"))
    if is_json_mime or is_json_chars:
        try:
            return json.loads(decoded_text)
        except Exception:
            try:
                return json_repair.repair_json(decoded_text, return_objects=True)
            except Exception:
                return decoded_text

    return decoded_text


def _parse_headers(headers_list: list[dict[str, Any]] | None) -> dict[str, str]:
    """Convert HAR header list [{'name': 'k', 'value': 'v'}] to dict."""
    result: dict[str, str] = {}
    if not headers_list:
        return result
    for h in headers_list:
        name = str(h.get("name", "")).strip()
        value = str(h.get("value", "")).strip()
        if name:
            result[name] = value
    return result


def _parse_query_params(
    query_list: list[dict[str, Any]] | None,
    url_query: str,
) -> dict[str, str]:
    """Combine queryString list from HAR with URL query string."""
    result: dict[str, str] = {}
    if url_query:
        for k, v in parse_qsl(url_query, keep_blank_values=True):
            result[k] = v
    if query_list:
        for q in query_list:
            name = str(q.get("name", "")).strip()
            value = str(q.get("value", "")).strip()
            if name:
                result[name] = value
    return result


def parse_har_data(  # noqa: C901, PLR0912, PLR0915
    data: dict[str, Any] | str | bytes,
    vendor: str = "sniffer",
    device_type: str = "iot",
    device_id: str = "obfuscated",
) -> CaptureDB:
    """Parse HAR dictionary or JSON bytes into a CaptureDB."""
    if isinstance(data, bytes):
        data = json.loads(data.decode("utf-8", errors="replace"))
    elif isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, dict):
        raise TypeError("Invalid HAR format: root must be a JSON object")

    log_data = data.get("log", data)
    entries = log_data.get("entries", [])
    if not isinstance(entries, list):
        raise TypeError("Invalid HAR format: log.entries must be a list")

    pairs: list[RawPairWithResponse] = []
    now = datetime.now(UTC)
    session_start = now

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue

        req = entry.get("request", {})
        resp = entry.get("response", {})
        if not req:
            continue

        # Timestamp
        entry_time_str = entry.get("startedDateTime")
        entry_dt = now
        if entry_time_str:
            try:
                clean_ts = entry_time_str.replace("Z", "+00:00")
                entry_dt = datetime.fromisoformat(clean_ts)
            except Exception:
                entry_dt = now

        if idx == 0:
            session_start = entry_dt

        # Parse Request
        raw_url = req.get("url", "/")
        url_parts = urlsplit(raw_url)
        path = url_parts.path or "/"
        if not path.startswith("/"):
            path = "/" + path

        method = str(req.get("method", "GET")).upper()
        req_headers = _parse_headers(req.get("headers"))
        req_query = _parse_query_params(req.get("queryString"), url_parts.query)

        # Request body
        post_data = req.get("postData", {})
        req_body = None
        if post_data:
            req_body = _parse_body(
                post_data.get("text"),
                post_data.get("mimeType"),
                post_data.get("encoding"),
            )

        # Parse Response
        raw_response = None
        if resp:
            status_code = int(resp.get("status", 200))
            resp_headers = _parse_headers(resp.get("headers"))
            resp_content = resp.get("content", {})
            resp_body = _parse_body(
                resp_content.get("text"),
                resp_content.get("mimeType"),
                resp_content.get("encoding"),
            )
            latency_ms = float(entry.get("time", 0.0) or 0.0)

            raw_response = RawResponse(
                status_code=status_code,
                headers=resp_headers,
                body=resp_body,
                latency_ms=latency_ms,
            )

        pair = RawPairWithResponse(
            pair_id=f"pair_{uuid.uuid4().hex[:12]}",
            timestamp=entry_dt,
            protocol="http",
            method=method,
            path=path,
            headers=req_headers,
            query_params=req_query,
            body=req_body,
            response=raw_response,
        )
        pairs.append(pair)

    session = CaptureSession(
        session_id=f"session_{uuid.uuid4().hex[:10]}",
        type="capture",
        timestamp_start=session_start,
        pairs=pairs,
    )

    meta = CaptureMeta(
        version=1,
        capture_id=f"cap_har_{uuid.uuid4().hex[:8]}",
        vendor=vendor,
        device_type=device_type,
        model="",
        firmware_version="",
        capture_date=session_start,
        description="Imported from HAR capture",
    )

    device_info = CaptureDeviceInfo(
        device_id=device_id,
        mac="obfuscated",
        serial="obfuscated",
    )

    return CaptureDB(
        meta=meta,
        device_info=device_info,
        sessions=[session],
    )
