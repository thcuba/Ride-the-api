"""
Backwards-compatibility shim for core.tls package.

All components have been modularized into ``core.tls``:
- ``core.tls.sni``: SNI parser
- ``core.tls.frida``: Frida script generator
- ``core.tls.models``: DecryptedRequest, DevicePortInfo, HTTP helpers
- ``core.tls.handshake``: SSLContext creation, ClientHello reassembly, TLS handshake pumping
- ``core.tls.http_stream``: HTTP/1.1 over TLS parsing with h11, response serialization,
  upstream forwarding
- ``core.tls.relay``: Non-decrypting TLS stream relay and live connection tracking
- ``core.tls.server``: TLSMITMServer, get_tls_mitm_server, peek_tls_mitm_server
"""

from __future__ import annotations

import asyncio  # Re-exported for test monkeypatching compatibility
from typing import Any

import core.tls.server as _server_mod
from core.cloud_forward import (  # Re-exported for test monkeypatching compatibility
    forward_intercepted,
)
from core.tls.frida import FRIDA_PINNING_BYPASS_SCRIPT, get_frida_script
from core.tls.handshake import (
    make_ssl_context,
    pump_tls_handshake,
    read_complete_client_hello,
)
from core.tls.http_stream import (
    forward_upstream,
    read_http_over_tls,
    write_http_response,
)
from core.tls.models import (
    MAX_ENCRYPTED_REQUEST_SIZE,
    MAX_REQUEST_SIZE,
    TLS_VERSION_MAP,
    DecryptedRequest,
    DevicePortInfo,
    parse_decrypted_http_request,
    serialize_http_response,
)
from core.tls.relay import (
    pipe_stream,
    relay_to_upstream,
    report_relayed,
)
from core.tls.server import (
    TLSMITMServer,
    _pipe_stream,
    get_tls_mitm_server,
    peek_tls_mitm_server,
)
from core.tls.sni import (
    MAX_CLIENT_HELLO_SIZE,
    TLS_CLIENT_HELLO,
    TLS_EXTENSION_SNI,
    TLS_HANDSHAKE,
    TLS_HANDSHAKE_HEADER_SIZE,
    TLS_MAX_RECORD_SIZE,
    TLS_RECORD_HEADER_SIZE,
    TLS_SNI_LIST_LENGTH_SIZE,
    extract_sni_from_client_hello,
)


def __getattr__(name: str) -> Any:
    if name == "_tls_mitm_server":
        return _server_mod._tls_mitm_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FRIDA_PINNING_BYPASS_SCRIPT",
    "MAX_CLIENT_HELLO_SIZE",
    "MAX_ENCRYPTED_REQUEST_SIZE",
    "MAX_REQUEST_SIZE",
    "TLS_CLIENT_HELLO",
    "TLS_EXTENSION_SNI",
    "TLS_HANDSHAKE",
    "TLS_HANDSHAKE_HEADER_SIZE",
    "TLS_MAX_RECORD_SIZE",
    "TLS_RECORD_HEADER_SIZE",
    "TLS_SNI_LIST_LENGTH_SIZE",
    "TLS_VERSION_MAP",
    "DecryptedRequest",
    "DevicePortInfo",
    "TLSMITMServer",
    "_pipe_stream",
    "asyncio",
    "extract_sni_from_client_hello",
    "forward_intercepted",
    "forward_upstream",
    "get_frida_script",
    "get_tls_mitm_server",
    "make_ssl_context",
    "parse_decrypted_http_request",
    "peek_tls_mitm_server",
    "pipe_stream",
    "pump_tls_handshake",
    "read_complete_client_hello",
    "read_http_over_tls",
    "relay_to_upstream",
    "report_relayed",
    "serialize_http_response",
    "write_http_response",
]
