"""Upstream forwarder, response replayer, and device connection trackers."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import dns.asyncquery
import dns.message
import dns.rdatatype
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from adapters.base import InterceptedRequest, ProtocolType, device_id_from_ip
from core.database import DeviceRegistry, RelayedConnection
from core.http_framing import servable_status, without_framing_headers
from core.traffic_selector import is_local_ip
from core.upstream_resolver import self_connection_reason

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

_RESPONSE_HOP_BY_HOP = ("content-length", "transfer-encoding", "connection")
_BODYLESS_STATUSES = frozenset({204, 304})
_MAX_MODIFICATION_DELAY_S = 30.0
_DEVICE_ID_HEADERS = (
    "x-device-id",
    "x-deviceid",
    "device-id",
    "deviceid",
    "x-client-id",
    "x-sn",
)
_MAX_CLAIMED_DEVICE_ID = 128
_LAST_SEEN_WRITE_INTERVAL_S = 60


def _get_server() -> ModuleType:
    from core import server  # noqa: PLC0415

    return server


def _get_db() -> object:
    return getattr(_get_server(), "db_manager", None)


def _get_orchestrator() -> object:
    return getattr(_get_server(), "orchestrator", None)


def _get_config_manager() -> object:
    cm = getattr(_get_server(), "config_manager", None)
    if cm is not None:
        return cm
    from core.config import get_config_manager  # noqa: PLC0415

    return get_config_manager()


def _get_modification_engine() -> object:
    getter = getattr(_get_server(), "get_modification_engine", None)
    if getter is not None:
        return getter()
    from core.modification import get_modification_engine  # noqa: PLC0415

    return get_modification_engine()


async def _resolve_device_for_ip(db: object, client_ip: str | None) -> str | None:
    """Resolve a source IP to a known device id, when it is bound."""
    if not client_ip or client_ip == "unknown" or not db:
        return None
    return await db.resolve_device_id(client_ip)  # type: ignore[union-attr]


def _last_seen_is_stale(stored: datetime | None, now: datetime) -> bool:
    """Say whether ``last_seen`` is old enough to be worth writing again."""
    if stored is None:
        return True
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    return (now - stored).total_seconds() >= _LAST_SEEN_WRITE_INTERVAL_S


async def handle_device_seen(client_ip: str, sni: str | None) -> None:
    """Register a device the moment it presents itself on a TLS port."""
    db_manager: object = _get_db()
    if not db_manager:
        return

    if self_connection_reason(client_ip):
        return

    device_id = device_id_from_ip("ip", client_ip)
    await db_manager.get_or_create_device(device_id, "unknown")  # type: ignore[union-attr]

    async with db_manager.core_session() as session:  # type: ignore[union-attr]
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if device is None:
            return

        changed = False
        known_ips = list(device.ip_addresses or [])
        if client_ip not in known_ips:
            known_ips.append(client_ip)
            device.ip_addresses = known_ips
            changed = True

        if sni:
            attributes = dict(device.extra_attributes or {})
            hostnames = list(attributes.get("seen_hostnames") or [])
            if sni not in hostnames:
                hostnames.append(sni)
                attributes["seen_hostnames"] = hostnames
                device.extra_attributes = attributes
                changed = True

        now = datetime.now(UTC)
        if _last_seen_is_stale(device.last_seen, now):
            device.last_seen = now
            changed = True

        if changed:
            await session.commit()
            db_manager.invalidate_ip_lookup_cache()  # type: ignore[union-attr]


async def should_relay_tls(client_ip: str) -> bool:
    """Say whether a connection must be forwarded untouched instead of decrypted."""
    db_manager: object = _get_db()
    if not db_manager:
        return False

    device_id = await db_manager.resolve_device_id(client_ip)  # type: ignore[union-attr]
    if not device_id:
        return False

    device = await db_manager.get_device(device_id)  # type: ignore[union-attr]
    if not device:
        return False

    return bool((device.extra_attributes or {}).get("tls_passthrough"))


async def mark_device_untrusted(client_ip: str, sni: str | None) -> None:
    """Record that a device refused our certificate, so it is relayed from now on."""
    db_manager: object = _get_db()
    if not db_manager:
        return

    if self_connection_reason(client_ip):
        return

    device_id = await db_manager.resolve_device_id(client_ip)  # type: ignore[union-attr]
    if not device_id:
        device_id = device_id_from_ip("ip", client_ip)
        await db_manager.get_or_create_device(device_id, "unknown")  # type: ignore[union-attr]

    async with db_manager.core_session() as session:  # type: ignore[union-attr]
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one_or_none()
        if device is None:
            return

        extra = dict(device.extra_attributes or {})
        now = datetime.now(UTC)

        previous = extra.get("tls_refusals")
        extra["tls_refusals"] = (previous if isinstance(previous, int) else 0) + 1
        extra["tls_last_refusal_at"] = now.isoformat()
        if not extra.get("tls_first_refusal_at"):
            extra["tls_first_refusal_at"] = now.isoformat()

        extra["tls_passthrough"] = True
        if not extra.get("tls_untrusted_reason"):
            extra["tls_untrusted_reason"] = "client_refused_certificate"

        hostnames = list(extra.get("seen_hostnames") or [])
        if sni and sni not in hostnames:
            hostnames.append(sni)

        extra["seen_hostnames"] = hostnames
        device.extra_attributes = extra
        await session.commit()

    db_manager.invalidate_device_cache(device_id)  # type: ignore[union-attr]
    logger.info(
        "TLS: %s refused our certificate (%d time(s)); from now on it is forwarded to its cloud "
        "undecrypted",
        client_ip,
        extra["tls_refusals"],
    )


async def record_relayed_connection(contact: dict) -> None:
    """Persist one connection that was forwarded without decryption."""
    db_manager: object = _get_db()
    if not db_manager:
        return

    client_ip = contact.get("client_ip") or ""
    if self_connection_reason(client_ip):
        return

    device_id = await db_manager.resolve_device_id(client_ip)  # type: ignore[union-attr]
    if not device_id:
        device_id = device_id_from_ip("ip", client_ip)
        await db_manager.get_or_create_device(device_id, "unknown")  # type: ignore[union-attr]

    async with db_manager.core_session() as session:  # type: ignore[union-attr]
        session.add(
            RelayedConnection(
                device_id=device_id,
                client_ip=client_ip,
                server_name=contact.get("server_name"),
                destination_port=int(contact.get("destination_port") or 0),
                upstream_ip=contact.get("upstream_ip"),
                bytes_to_cloud=int(contact.get("bytes_to_cloud") or 0),
                bytes_to_device=int(contact.get("bytes_to_device") or 0),
                duration_ms=int(contact.get("duration_ms") or 0),
                outcome=contact.get("outcome") or "relayed",
            )
        )
        await session.commit()


async def resolve_upstream(host: str) -> str | None:
    """Resolve ``host`` through the configured upstream resolvers."""
    config_mgr = _get_config_manager()
    config = getattr(config_mgr, "config", None)
    if not config:
        return None
    servers = [*config.dns.dns_servers, *config.dns.dns_servers_v6]
    if not servers:
        return None

    query = dns.message.make_query(host, dns.rdatatype.A)
    for server in servers:
        try:
            response = await dns.asyncquery.udp(query, server, timeout=3)
        except Exception:
            continue
        for answer in response.answer:
            for item in answer:
                if item.rdtype == dns.rdatatype.A:
                    return str(item)
    return None


def _local_http_response(response: dict) -> Response:
    """Serve a locally built ``{status_code, headers, body}`` response."""
    status = servable_status(response.get("status_code"))
    headers = without_framing_headers(response.get("headers"))
    if status in _BODYLESS_STATUSES:
        return Response(status_code=status, headers=headers)
    return JSONResponse(status_code=status, content=response.get("body", {}), headers=headers)


def _upstream_response(cloud_response: object, decoded: dict | None = None) -> Response:
    """Replay an upstream response verbatim: status, headers and body bytes."""
    body = getattr(cloud_response, "body_bytes", None)
    if body is None:
        fallback = (
            decoded if decoded is not None else (getattr(cloud_response, "response", None) or {})
        )
        body = json.dumps(fallback).encode("utf-8")

    headers = {
        str(k): str(v)
        for k, v in (getattr(cloud_response, "response_headers", None) or {}).items()
        if str(k).lower() not in _RESPONSE_HOP_BY_HOP
    }
    headers.setdefault("content-type", "application/json")
    return Response(
        content=body,
        status_code=getattr(cloud_response, "status_code", None) or 200,
        headers=headers,
    )


def _raw_target(request: Request) -> str | None:
    """``/{path}?{query}`` exactly as the client sent it, minus the vendor prefix."""
    raw_path = request.scope.get("raw_path")
    if not raw_path:
        return None

    parts = raw_path.decode("latin-1").split("/", 2)
    target = "/" + (parts[2] if len(parts) > 2 else "")  # noqa: PLR2004
    query = request.scope.get("query_string") or b""
    return f"{target}?{query.decode('latin-1')}" if query else target


async def _forward_bypassed_request(  # noqa: PLR0913
    adapter: object,
    request: Request,
    path: str,
    body: object,
    client_ip: str,
    *,
    raw_body: bytes | None = None,
) -> Response:
    """Forward a bypassed device's request straight to the cloud."""
    config_mgr = _get_config_manager()
    config = getattr(config_mgr, "config", None)
    if config and config.learning.signal_forward_to_cloud:
        return JSONResponse(
            status_code=502,
            content={"action": "forward", "reason": "bypass"},
            headers={"X-Action": "forward", "X-Original-Host": str(request.url)},
        )

    intercepted = InterceptedRequest(
        device_id="",
        timestamp=datetime.now(UTC),
        protocol=(ProtocolType.HTTPS if request.url.scheme == "https" else ProtocolType.HTTP),
        method=request.method,
        path=f"/{path}",
        headers=dict(request.headers),
        query_params=dict(request.query_params),
        body=body,
        body_bytes=raw_body,
        client_ip=client_ip,
        raw_target=_raw_target(request),
    )
    cloud_response = await adapter.forward_to_cloud(intercepted)  # type: ignore[union-attr]

    if cloud_response and cloud_response.success:
        return _upstream_response(cloud_response)

    return JSONResponse(
        status_code=502,
        content={
            "error": "Cloud passthrough failed",
            "detail": str(cloud_response.error) if cloud_response else "No response",
        },
    )


async def _discard_pending_capture(
    device_id: str, result: dict | None, *, count_as_error: bool = True
) -> None:
    """Drop the request captured for ``result`` if its response will never be paired."""
    key = (result or {}).get("correlation_key")
    orchestrator = _get_orchestrator()
    if not key or not device_id or not orchestrator:
        return

    try:
        await orchestrator.discard_request(device_id, key, count_as_error=count_as_error)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        logger.warning("Could not discard pending capture %s", key, exc_info=True)


async def _modification_delay(delay_ms: object) -> None:
    """Honour a DELAY modification rule."""
    try:
        seconds = float(delay_ms) / 1000  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return

    if seconds > 0:
        await asyncio.sleep(min(seconds, _MAX_MODIFICATION_DELAY_S))


def _apply_response_modifications(intercepted: InterceptedRequest, response: dict) -> dict:
    """Apply on-the-fly response modification rules, normalising to a wrapper."""
    engine = _get_modification_engine()
    modified, _was_modified = engine.process_response(intercepted, response)  # type: ignore[union-attr]
    if isinstance(modified, dict) and "status_code" in modified:
        return modified

    out = dict(response)
    if modified is not None:
        if isinstance(modified, dict):
            modified = {k: v for k, v in modified.items() if k != "modifications"}
        out["body"] = modified
    return out


async def _get_request_body(request: Request) -> dict | None:
    """Extract JSON body from request."""
    try:
        body = await request.body()
        if not body:
            return None
        i = 0
        n = len(body)
        while i < n and body[i] in b" \t\r\n":
            i += 1
        if i >= n or body[i] not in (123, 91):  # ord('{') == 123, ord('[') == 91
            return None
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug("Request body is not JSON: %s", e)
        return None


def _is_local_ip(ip: str) -> bool:
    """Check if IP is in a private/local range."""
    return is_local_ip(ip)


def _extract_device_id(headers: dict, path: str) -> str:
    """Try to extract device ID from headers or path (never empty)."""
    for header in _DEVICE_ID_HEADERS:
        value = (headers.get(header) or "").strip()
        if 0 < len(value) <= _MAX_CLAIMED_DEVICE_ID:
            return value

    parts = path.strip("/").split("/")
    for part in parts:
        if len(part) >= 8 and part.isalnum():  # noqa: PLR2004
            return part
    return "unknown"
