"""TLS handshake, SSLContext creation and ClientHello reassembly."""

from __future__ import annotations

import asyncio
import logging
import ssl
import tempfile

from core.tls.sni import (
    MAX_CLIENT_HELLO_SIZE,
    _client_hello_message,
)

logger = logging.getLogger(__name__)


async def read_complete_client_hello(reader: asyncio.StreamReader, initial_data: bytes) -> bytes:
    """Collect a ClientHello split across TCP reads without dropping bytes."""
    data = bytearray(initial_data[:MAX_CLIENT_HELLO_SIZE])
    while len(data) < MAX_CLIENT_HELLO_SIZE:
        complete, _message = _client_hello_message(data)
        if complete:
            break
        more = await reader.read(min(4096, MAX_CLIENT_HELLO_SIZE - len(data)))
        if not more:
            break
        data.extend(more)
    return bytes(data)


def make_ssl_context(
    cert_pem: str,
    key_pem: str,
    min_tls_version: ssl.TLSVersion,
    max_tls_version: ssl.TLSVersion,
    cache: dict[tuple[str, str], ssl.SSLContext] | None = None,
) -> tuple[ssl.SSLContext | None, str | None]:
    """Create an SSL context with the given cert and key.

    Returns (context, temp_file_path) or (None, None) on failure.
    The temp file must be cleaned up after use.

    Performance: contexts are cached per ``(cert_pem, key_pem)`` so the
    temp-file write + PEM parse + context construction only happens once
    per distinct certificate instead of on every TLS connection. A cached
    context returns a ``None`` temp path (nothing to clean up). Cert
    rotation changes the PEM, so the key changes and a fresh context is
    built for the new certificate.
    """
    cache_key = (cert_pem, key_pem)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached, None
    try:
        # Write cert+key to a temp file (load_cert_chain only takes file paths)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".pem",
            delete=False,
        ) as f:
            f.write(cert_pem)
            f.write("\n")
            f.write(key_pem)
            temp_path = f.name

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(temp_path)

        # Security settings
        ctx.minimum_version = min_tls_version
        ctx.maximum_version = max_tls_version
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # load_cert_chain parses the file eagerly, so the context is
        # self-contained and the temp file may be cleaned up immediately.
        if cache is not None:
            cache[cache_key] = ctx
        return ctx, temp_path  # noqa: TRY300
    except Exception:
        logger.exception("TLS MITM: failed to create SSL context")
        return None, None


async def pump_tls_handshake(  # noqa: PLR0913, PLR0917
    ssl_obj: ssl.SSLObject,
    incoming: ssl.MemoryBIO,
    outgoing: ssl.MemoryBIO,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeout: float = 30.0,
    max_attempts: int = 100,
) -> bool:
    """Complete the TLS handshake, reading/writing as needed."""
    handshake_attempts = 0

    while handshake_attempts < max_attempts:
        handshake_attempts += 1
        try:
            ssl_obj.do_handshake()
            # Drain any remaining handshake output
            if outgoing.pending:
                writer.write(outgoing.read(4096))
                await writer.drain()
            return True  # Handshake complete  # noqa: TRY300
        except ssl.SSLWantReadError:
            # Send any pending handshake output to the client
            if outgoing.pending:
                writer.write(outgoing.read(4096))
                await writer.drain()
            # Read more data from the client
            try:
                more = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            except TimeoutError:
                logger.warning("TLS MITM: handshake timeout")
                return False
            if not more:
                return False
            incoming.write(more)
        except ssl.SSLWantWriteError:
            if outgoing.pending:
                writer.write(outgoing.read(4096))
                await writer.drain()
        except ssl.SSLError as e:
            logger.warning("TLS MITM: handshake failed: %s", e)
            return False

    logger.warning("TLS MITM: handshake exceeded max attempts")
    return False
