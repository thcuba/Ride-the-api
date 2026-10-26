"""A privileged-port failure must come with the fix, not only with the error.

Binding 443 needs CAP_NET_BIND_SERVICE, and the failure mode is nasty: it reads as "the port
is taken", and the obvious fix (`setcap`) refuses the virtualenv's ``bin/python`` because it
is a symlink. Both traps are covered here, plus the fact that /health now reports what is
bound instead of leaving an operator to guess.
"""

from __future__ import annotations

import errno
import socket

import pytest
from fastapi.testclient import TestClient

from core.ports import bind_failure_hint, interpreter_path, is_privileged_port
from core.server import app


def test_privileged_ports_are_recognised():
    assert is_privileged_port(53) is True
    assert is_privileged_port(443) is True
    assert is_privileged_port(1024) is False
    assert is_privileged_port(8443) is False


def test_a_permission_failure_names_the_fix_and_the_real_interpreter():
    hint = bind_failure_hint(443, PermissionError(errno.EACCES, "Permission denied"))

    assert "setcap cap_net_bind_service=+ep" in hint
    assert interpreter_path() in hint  # the resolved interpreter, never the venv symlink
    assert "symlink" in hint  # because setcap refuses one, which cost real time to learn
    assert "net.ipv4.ip_unprivileged_port_start" in hint  # the alternative


def test_a_taken_port_is_not_confused_with_a_privileged_one():
    """A port in use is a different problem: the capability advice would mislead."""
    assert bind_failure_hint(443, OSError(errno.EADDRINUSE, "Address already in use")) == ""
    assert bind_failure_hint(8443, PermissionError(errno.EACCES, "Permission denied")) == ""


def test_the_hint_comes_from_a_real_failure():
    """The measured case on the proxy, reproduced here when the capability is absent."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 443))
    except OSError as exc:
        hint = bind_failure_hint(443, exc)
        assert "setcap cap_net_bind_service=+ep" in hint
    else:
        pytest.skip("bound 443 (running with the capability or as root): nothing to diagnose")
    finally:
        sock.close()


def test_health_reports_what_is_bound():
    ports = TestClient(app).get("/health").json()["ports"]

    assert set(ports) == {"proxy", "dns", "tls_mitm"}
    assert ports["proxy"]["listening"] is True
    # with no server started, the honest answer is "nothing bound", not an optimistic one
    assert ports["dns"]["listening"] is False
    assert ports["tls_mitm"]["missing"] == ports["tls_mitm"]["requested"]
