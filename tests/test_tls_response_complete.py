"""Una risposta grande deve arrivare al device **intera**.

`write_http_response` svuotava l'uscita TLS con una **sola** `read(65536)`. `SSLObject.write`
spezza il plaintext in record da al massimo 16 KiB e `MemoryBIO.read` restituisce solo ciò
che c'è in quel momento: il resto rimaneva cifrato dentro il BIO, la funzione usciva e la
connessione si chiudeva subito dopo. Misurato sul filo: risposte vendor oltre ~49 KB
arrivavano mozzate, con connessione chiusa subito dopo.

Il secondo difetto era accanto al primo: `SSLWantWriteError` (BIO pieno) veniva ingoiato
con un `pass`, quindi il pezzo che non era entrato nel BIO veniva perso in silenzio.
"""

import json
import ssl

import pytest

from core.tls.http_stream import write_http_response

END_MARKER = "FINE-DEL-CORPO"


class _FakeOutgoing:
    """Il MemoryBIO di uscita: `read` consegna al massimo quello che chiedi."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    @property
    def pending(self) -> int:
        return len(self.buffer)

    def read(self, size: int) -> bytes:
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        return chunk


class _FakeSSLObject:
    """Un SSLObject che produce record da 16 KiB, come quello vero.

    Tiene una capacità: oltre quella solleva SSLWantWriteError, che è il caso che il
    codice ingoiava con `pass`.
    """

    RECORD = 16 * 1024

    def __init__(self, outgoing: _FakeOutgoing, capacity: int = 10**9) -> None:
        self.outgoing = outgoing
        self.capacity = capacity
        self.accepted = 0

    def write(self, data: bytes) -> int:
        room = self.capacity - self.outgoing.pending
        if room <= 0:
            raise ssl.SSLWantWriteError()
        take = min(len(data), room, self.RECORD)
        self.outgoing.buffer.extend(data[:take])
        self.accepted += take
        return take


class _FakeWriter:
    def __init__(self) -> None:
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        self.sent.extend(data)

    async def drain(self) -> None:
        return None


def _response(body: bytes) -> dict:
    return {
        "status_code": 200,
        "headers": {"content-type": "application/json", "content-length": str(len(body))},
        "body": body,
    }


@pytest.mark.asyncio
async def test_a_large_response_reaches_the_device_whole():
    """Il caso che falliva: 200 KB con la coda oltre il primo read."""
    payload = {"data": "x" * 200_000, "tail": END_MARKER}
    body = json.dumps(payload).encode()
    assert len(body) > 65536  # noqa: PLR2004

    outgoing = _FakeOutgoing()
    ssl_obj = _FakeSSLObject(outgoing)
    writer = _FakeWriter()

    await write_http_response(ssl_obj, outgoing, writer, _response(body), "192.168.1.50")

    sent = bytes(writer.sent)
    assert END_MARKER.encode() in sent, "la coda della risposta non è arrivata"
    assert sent.endswith(body), "l'ultimo pezzo del corpo deve essere l'ultimo scritto"
    assert outgoing.pending == 0, "il BIO deve restare vuoto: niente resta indietro"
    assert len(sent) > len(body)


@pytest.mark.asyncio
async def test_a_full_bio_is_drained_and_the_write_retried():
    """`SSLWantWriteError` non è la fine della risposta: è l'ordine di svuotare."""
    body = b'{"data": "' + b"y" * 100_000 + b'"}'
    outgoing = _FakeOutgoing()
    # Capacità piccola: il primo write non entra tutto, e senza retry si perdeva il resto.
    ssl_obj = _FakeSSLObject(outgoing, capacity=8192)
    writer = _FakeWriter()

    await write_http_response(ssl_obj, outgoing, writer, _response(body), "192.168.1.50")

    sent = bytes(writer.sent)
    assert sent.endswith(body), "il corpo deve arrivare intero anche partendo da un BIO pieno"
    assert outgoing.pending == 0


@pytest.mark.asyncio
async def test_a_small_response_is_still_correct():
    """Nessuna regressione sul caso normale."""
    body = b'{"ok": true}'
    outgoing = _FakeOutgoing()
    ssl_obj = _FakeSSLObject(outgoing)
    writer = _FakeWriter()

    await write_http_response(ssl_obj, outgoing, writer, _response(body), "192.168.1.50")

    sent = bytes(writer.sent)
    assert sent.endswith(body)
    assert b"200 OK" in sent
    assert b"content-length: 12" in sent.lower() or b"content-length:12" in sent.lower()


@pytest.mark.asyncio
async def test_a_json_body_is_serialized_once():
    """Il corpo non-stringa continua a essere serializzato in JSON."""
    outgoing = _FakeOutgoing()
    ssl_obj = _FakeSSLObject(outgoing)
    writer = _FakeWriter()

    await write_http_response(
        ssl_obj,
        outgoing,
        writer,
        {"status_code": 200, "headers": {"content-type": "application/json"}, "body": {"ok": True}},
        "192.168.1.50",
    )

    assert b'{"ok": true}' in bytes(writer.sent)
