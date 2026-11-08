"""
Low-level TLS ClientHello parser for zero-overhead SNI extraction.

Parses binary TLS records without external crypto libraries to extract
the Server Name Indication (SNI) extension from the handshake payload.
"""

from __future__ import annotations

import struct

# TLS record & handshake constants
TLS_HANDSHAKE = 0x16
TLS_CLIENT_HELLO = 0x01
TLS_EXTENSION_SNI = 0x0000
TLS_RECORD_HEADER_SIZE = 5
TLS_HANDSHAKE_HEADER_SIZE = 4
TLS_MAX_RECORD_SIZE = 18432
TLS_SNI_LIST_LENGTH_SIZE = 2
MAX_CLIENT_HELLO_SIZE = 64 * 1024


def extract_sni_from_client_hello(data: bytes) -> str | None:  # noqa: C901, PLR0911, PLR0912
    """Extract the SNI from a complete ClientHello, including split TLS records.

    TCP may split a record anywhere, and a handshake message may span records.
    ``_handle_connection`` first assembles the full bounded ClientHello; this
    parser then reassembles its handshake payload before reading extensions.
    """
    complete, message = _client_hello_message(data)
    if not complete or message is None:
        return None

    hello = message[4:]
    pos = 34  # legacy version (2 bytes) + random (32 bytes)
    if pos >= len(hello):
        return None

    session_id_len = hello[pos]
    pos += 1 + session_id_len
    if pos + 2 > len(hello):
        return None

    cipher_len = struct.unpack("!H", hello[pos : pos + 2])[0]
    pos += 2 + cipher_len
    if pos >= len(hello):
        return None

    compression_len = hello[pos]
    pos += 1 + compression_len
    if pos + 2 > len(hello):
        return None

    extensions_len = struct.unpack("!H", hello[pos : pos + 2])[0]
    pos += 2
    extensions_end = pos + extensions_len
    if extensions_end > len(hello):
        return None

    while pos + 4 <= extensions_end:
        extension_type = struct.unpack("!H", hello[pos : pos + 2])[0]
        extension_len = struct.unpack("!H", hello[pos + 2 : pos + 4])[0]
        pos += 4
        extension_end = pos + extension_len
        if extension_end > extensions_end:
            return None

        if extension_type == TLS_EXTENSION_SNI and extension_len >= TLS_SNI_LIST_LENGTH_SIZE:
            name_list_len = struct.unpack("!H", hello[pos : pos + 2])[0]
            name_pos = pos + 2
            name_list_end = name_pos + name_list_len
            if name_list_end > extension_end:
                return None
            while name_pos + 3 <= name_list_end:
                name_type = hello[name_pos]
                name_len = struct.unpack("!H", hello[name_pos + 1 : name_pos + 3])[0]
                name_pos += 3
                if name_pos + name_len > name_list_end:
                    return None
                if name_type == 0x00 and name_len:
                    return hello[name_pos : name_pos + name_len].decode("utf-8", errors="replace")
                name_pos += name_len
            return None

        pos = extension_end

    return None


def _client_hello_message(data: bytes) -> tuple[bool, bytes | None]:  # noqa: PLR0911
    """Return whether a bounded ClientHello is complete and its handshake bytes.

    ``(False, None)`` means more TCP bytes are needed. A complete but malformed
    or non-ClientHello record returns ``(True, None)`` so callers do not wait on
    data that cannot provide an SNI.
    """
    handshake = bytearray()
    record_pos = 0
    while True:
        if len(data) - record_pos < TLS_RECORD_HEADER_SIZE:
            return False, None
        if data[record_pos] != TLS_HANDSHAKE:
            return True, None

        record_len = struct.unpack("!H", data[record_pos + 3 : record_pos + 5])[0]
        if record_len == 0 or record_len > TLS_MAX_RECORD_SIZE:
            return True, None
        record_end = record_pos + TLS_RECORD_HEADER_SIZE + record_len
        if record_end > len(data):
            return False, None

        handshake.extend(data[record_pos + TLS_RECORD_HEADER_SIZE : record_end])
        if handshake and handshake[0] != TLS_CLIENT_HELLO:
            return True, None
        if len(handshake) >= TLS_HANDSHAKE_HEADER_SIZE:
            hello_len = (handshake[1] << 16) | (handshake[2] << 8) | handshake[3]
            message_len = TLS_HANDSHAKE_HEADER_SIZE + hello_len
            if message_len > MAX_CLIENT_HELLO_SIZE:
                return True, None
            if len(handshake) >= message_len:
                return True, bytes(handshake[:message_len])
        record_pos = record_end
