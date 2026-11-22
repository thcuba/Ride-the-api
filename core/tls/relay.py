"""Non-decrypting TLS stream relay and live connection tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from itertools import count

logger = logging.getLogger(__name__)


async def pipe_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    counter: dict[str, int] | None = None,
) -> None:
    """Copy one stream to the other verbatim, until either side closes.

    Used only by the relay path, where nothing is decrypted and nothing is inspected. ``counter``
    accumulates the bytes moved: with the payload unreadable, volume is one of the few facts left.
    """
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            if counter is not None:
                counter["bytes"] = counter.get("bytes", 0) + len(chunk)
    except (OSError, TimeoutError):
        return
    finally:
        with suppress(Exception):
            writer.write_eof()


# Backwards compatibility alias
_pipe_stream = pipe_stream


async def report_relayed(on_relayed: Callable[[dict], Coroutine] | None, contact: dict) -> None:
    """Hand one relayed contact to the recorder, best effort.

    Recording must never affect the forwarding itself: a failure here is logged and the
    connection carries on, because staying connected matters more than being counted.
    """
    if on_relayed is None:
        return
    try:
        await on_relayed(contact)
    except Exception:
        logger.warning(
            "TLS relay: could not record the contact for %s",
            contact.get("client_ip"),
            exc_info=True,
        )


async def relay_to_upstream(  # noqa: PLR0913, PLR0917 — streams, hello, sni, peer, port
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    client_hello: bytes,
    sni: str,
    client_ip: str,
    dst_port: int,
    resolve_upstream: Callable[[str], Coroutine] | None,
    report_relayed_fn: Callable[[dict], Coroutine],
    register_live_relay: Callable[[str, dict], Coroutine],
    finish_live_relay: Callable[[str], None],
    relay_sequence: count,
) -> None:
    """Forward a connection to the real cloud without terminating TLS.

    For devices that pin their vendor's certificate -- they will refuse any certificate we
    generate, so the only useful thing left is to stay out of the way. The ClientHello already
    read is replayed to the real server and the two streams are then piped verbatim: the device
    keeps talking to its cloud, nothing is decrypted, and its identity is already in the
    registry.
    """
    base = {"client_ip": client_ip, "server_name": sni, "destination_port": dst_port}

    if resolve_upstream is None:
        logger.warning("TLS relay: no upstream resolver wired, dropping %s", client_ip)
        await report_relayed_fn({**base, "outcome": "no_resolver"})
        writer.close()
        return

    try:
        upstream_ip = await resolve_upstream(sni)
    except Exception:
        logger.warning("TLS relay: could not resolve %s", sni, exc_info=True)
        upstream_ip = None

    if not upstream_ip:
        logger.warning("TLS relay: %s does not resolve, dropping %s", sni, client_ip)
        await report_relayed_fn({**base, "outcome": "unresolvable"})
        writer.close()
        return

    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_ip, dst_port), timeout=10
        )
    except (OSError, TimeoutError) as exc:
        logger.warning("TLS relay: cannot reach %s:%s (%s)", upstream_ip, dst_port, exc)
        await report_relayed_fn({**base, "upstream_ip": upstream_ip, "outcome": "unreachable"})
        writer.close()
        return

    logger.info(
        "TLS relay: %s -> %s (%s:%s), forwarded without decryption",
        client_ip,
        sni,
        upstream_ip,
        dst_port,
    )
    to_cloud: dict[str, int] = {}
    to_device: dict[str, int] = {}
    started = time.monotonic()
    up_writer.write(client_hello)
    to_cloud["bytes"] = len(client_hello)
    # Visible from here on: a session that stays open for hours must not look like silence.
    relay_key = f"{client_ip}:{dst_port}:{next(relay_sequence)}"
    await register_live_relay(
        relay_key,
        {
            **base,
            "upstream_ip": upstream_ip,
            "to_cloud": to_cloud,
            "to_device": to_device,
        },
    )
    try:
        await up_writer.drain()
        await asyncio.gather(
            pipe_stream(reader, up_writer, to_cloud),
            pipe_stream(up_reader, writer, to_device),
            return_exceptions=True,
        )
    finally:
        # The contact record below becomes the visible trace of this session, so the live view
        # goes away in the same breath.
        finish_live_relay(relay_key)
        for stream in (up_writer, writer):
            with suppress(Exception):
                stream.close()
        await report_relayed_fn(
            {
                **base,
                "upstream_ip": upstream_ip,
                "bytes_to_cloud": to_cloud.get("bytes", 0),
                "bytes_to_device": to_device.get("bytes", 0),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "outcome": "relayed",
            },
        )
