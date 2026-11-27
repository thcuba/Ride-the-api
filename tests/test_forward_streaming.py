"""Una risposta più grande della memoria: dallo spool al device, senza 502.

Il forward accumulava il corpo in RAM con un tetto di 5 MiB e oltre quella soglia
sollevava `CloudForwardError`: un download di firmware riceveva **502** e il device non
aveva il file. Il tetto era una difesa contro l'uso di memoria, ma la risposta giusta a
"non ci sta in RAM" non è "allora non te la do".

Qui si verifica il percorso vero, con un upstream HTTP reale su localhost: oltre la
soglia il corpo finisce in uno spool su disco, `body` resta vuoto e `body_file` è quello
che il chiamante consuma. Il test controllo anche che il contenuto sia **completo**:
sarebbe facile spostare il problema dalla RAM al disco e troncare comunque.
"""

import asyncio
import hashlib

import pytest

from core.cloud_forward import (
    _MAX_RESPONSE_BODY_IN_MEMORY,
    CloudForwarder,
)

BIG = 6 * 1024 * 1024  # oltre la soglia in memoria


async def _serve(response_bytes: bytes, ready: asyncio.Event) -> None:
    """Un upstream HTTP/1.1 minimo: legge la richiesta, scrive la risposta, chiude."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(response_bytes)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        ready.set()

    return handle


async def _fetch(response_bytes: bytes) -> tuple[object, asyncio.base_events.Server]:
    ready = asyncio.Event()
    server = await asyncio.start_server(await _serve(response_bytes, ready), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    forwarder = CloudForwarder(timeout=20, connect_timeout=5)
    result = await forwarder.forward(
        hostname="127.0.0.1",
        ip="127.0.0.1",
        port=port,
        method="GET",
        path="/firmware.bin",
        use_tls=False,
    )
    server.close()
    await server.wait_closed()
    return result, server


def _http_ok(body: bytes) -> bytes:
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: application/octet-stream\r\n"
        b"content-length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    return head + body


@pytest.mark.asyncio
async def test_a_body_over_the_memory_limit_is_spooled_not_refused():
    """Il difetto: prima era `CloudForwardError` → il device riceveva 502."""
    body = bytes(range(256)) * (BIG // 256)
    result, _ = await _fetch(_http_ok(body))

    assert result.body == b"", "un corpo così non va tenuto in memoria"
    assert result.body_file is not None, "deve arrivare come spool, non come errore"
    try:
        streamed = result.body_file.read()
    finally:
        result.body_file.close()

    assert len(streamed) == len(body), "il corpo deve arrivare intero, non troncato"
    assert hashlib.sha256(streamed).hexdigest() == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_a_small_body_still_comes_back_in_memory():
    """Nessuna regressione: sotto la soglia il contratto è quello di sempre."""
    body = b'{"status": "ok"}'
    result, _ = await _fetch(_http_ok(body))

    assert result.body == body
    assert result.body_file is None


@pytest.mark.asyncio
async def test_the_threshold_is_the_one_documented():
    """La soglia in memoria è 5 MiB: se cambia, questo test lo dice."""
    assert _MAX_RESPONSE_BODY_IN_MEMORY == 5 * 1024 * 1024  # noqa: PLR2004
