"""Guard for the ``base_url`` a client hands us for an LLM provider.

A profile's base URL arrives from the request body — the dashboard's Test button sends
the form values before they are saved — so without a check the server opens a connection
wherever it is told: ``file://`` paths, the cloud metadata endpoint, multicast addresses,
a bare ``0.0.0.0``. That is the SSRF surface of the LLM settings.

What is **allowed** matters as much as what is blocked: a provider legitimately lives on
loopback (``http://127.0.0.1:11434/v1`` for Ollama) or on the LAN, so loopback and
private addresses pass. Blocked are schemes other than http/https; unspecified, multicast,
reserved and link-local addresses — a link-local endpoint is the cloud metadata service,
never a model; and hostnames that resolve **only** to those, which is how
``metadata.google.internal`` slips past a check on the literal text alone.

The check is deliberately not a whitelist: the proxy runs on a home LAN where the useful
providers are self-hosted, and a whitelist would have to be edited to add one.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class LLMUrlRejected(ValueError):
    """The base URL cannot be a provider, and must not be contacted."""


def _blocked_reason(host: str) -> str | None:
    """Why this literal address must not be contacted, or ``None`` if it is not one."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # a name, not a literal: resolved by the caller
    if addr.is_loopback:
        # Returned before the checks below: Python reports the IPv6 loopback (::1) as
        # reserved too, and a provider on this very host is the normal case (Ollama).
        return None
    for flagged, reason in (
        (addr.is_link_local, "is link-local (the cloud metadata endpoint lives there)"),
        (addr.is_unspecified, "does not name a host"),
        (addr.is_multicast, "is a multicast group"),
        (addr.is_reserved, "is reserved"),
    ):
        if flagged:
            return f"{addr} {reason}"
    return None


def reject_reason(base_url: str) -> str | None:
    """Why ``base_url`` must be refused on its own text alone, or ``None``.

    Only the parts visible in the string: scheme and a literal host. A hostname is
    checked by :func:`check_base_url`, which can resolve it.
    """
    raw = (base_url or "").strip()
    if not raw:
        return "the base URL is empty"
    parts = urlsplit(raw)
    if parts.scheme not in _ALLOWED_SCHEMES:
        return (
            f"scheme '{parts.scheme or '(none)'}' is not supported: "
            f"only {' and '.join(sorted(_ALLOWED_SCHEMES))} can reach a provider"
        )
    host = parts.hostname
    if not host:
        return "the base URL has no host"
    return _blocked_reason(host)


async def check_base_url(base_url: str) -> None:
    """Raise :class:`LLMUrlRejected` when ``base_url`` must not be contacted.

    A name is resolved so that a name pointing only at blocked addresses is refused too.
    An unresolvable name is *allowed*: the connection then fails on its own, and there is
    nothing to reach — refusing it would only turn a typo into a security-looking error.
    """
    reason = reject_reason(base_url)
    if reason is not None:
        raise LLMUrlRejected(f"Refused base URL {base_url!r}: {reason}")

    host = urlsplit(base_url.strip()).hostname or ""
    if _is_literal(host):
        return

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            None,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except OSError:
        return

    addresses = {str(info[4][0]) for info in infos if info[4]}
    blocked = {address: _blocked_reason(address) for address in addresses}
    if addresses and all(blocked.values()):
        first = next(iter(blocked.values()))
        raise LLMUrlRejected(f"Refused base URL {base_url!r}: {host} resolves to {first}")


def _is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


__all__ = ["LLMUrlRejected", "check_base_url", "reject_reason"]
