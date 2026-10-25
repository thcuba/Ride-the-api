"""Diagnostics for a port a process may not bind without privilege.

Binding a port below 1024 needs ``CAP_NET_BIND_SERVICE``. Two things make that failure
expensive to diagnose in practice:

* it looks like "the port is taken", so the first instinct is to hunt for a conflicting
  process that does not exist;
* the fix is easy to apply to the wrong file — a virtualenv's ``bin/python`` is a *symlink*,
  and ``setcap`` refuses symlinks ("Invalid file ... for capability operation"), so the
  capability has to go on the interpreter it points to.

The hint below is what turns a bare ``[Errno 13] Permission denied`` into something an
operator can act on.
"""

from __future__ import annotations

import errno
import os
import sys

PRIVILEGED_PORT_LIMIT = 1024


def is_privileged_port(port: int) -> bool:
    """Is this port below the unprivileged limit?"""
    return port < PRIVILEGED_PORT_LIMIT


def interpreter_path() -> str:
    """The real interpreter a capability must be applied to.

    ``sys.executable`` inside a virtualenv is a symlink (``bin/python -> python3 ->
    /usr/bin/python3.x``); ``setcap`` rejects symlinks, so resolve it first.
    """
    return os.path.realpath(sys.executable)


def bind_failure_hint(port: int, exc: BaseException) -> str:
    """A line an operator can act on, or ``""`` when there is nothing useful to say.

    Only a *permission* failure on a privileged port deserves the capability advice: a port
    that is already in use is a different problem and must not be papered over with it.
    """
    if not is_privileged_port(port) or not isinstance(exc, OSError):
        return ""
    if exc.errno not in (errno.EACCES, errno.EPERM):
        return ""
    return (
        f"port {port} needs privilege: run "
        f"'sudo setcap cap_net_bind_service=+ep {interpreter_path()}' — the capability goes "
        f"on the real interpreter, because a virtualenv's bin/python is a symlink and setcap "
        f"refuses symlinks (readlink -f gives the target); the alternative is lowering "
        f"net.ipv4.ip_unprivileged_port_start"
    )
