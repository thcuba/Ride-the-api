"""DNS discovery and interception routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.dns_discovery import get_dns_recorder
from core.dns_server import get_dns_interceptor

if TYPE_CHECKING:
    from core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dns"])


def _server():
    from core import server  # noqa: PLC0415 - lazy import avoids a cycle with core.server

    return server


def _clean_dns_hostname(value: object) -> str | None:
    """Normalise an operator-supplied name: a bare DNS name or a wildcard pattern.

    Rejects anything that is not a plausible name (letters, digits and the
    wildcard/dot characters), so the intercept list cannot collect entries that
    would never match a query. A bare address is refused too: an IP literal is
    not a name the DNS can answer for.
    """
    if not isinstance(value, str):
        return None
    name = value.strip().lower().rstrip(".")
    if not name or len(name) > 253:  # noqa: PLR2004 - DNS name limit
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.*?_-")
    if any(ch not in allowed for ch in name):
        return None
    if name.replace(".", "").isdigit():
        return None
    return name


def _dns_policy_from_body(body: dict) -> tuple[dict, str | None]:
    """Validate a DNS settings body: (``{intercept, exclude, intercept_all}``, error).

    Only the fields actually present are returned (``None`` for the others), so the
    dashboard can flip one switch without resending the rest. A field that is not a
    list, or that carries something which is not a name, is refused rather than
    silently dropped: a typo must not look like it was saved. Duplicates collapse,
    so what ends up in the config is a set of rules.
    """
    policy: dict = {"intercept": None, "exclude": None, "intercept_all": None}
    for field in ("intercept", "exclude"):
        raw = body.get(field)
        if raw is None:
            continue
        if not isinstance(raw, list):
            return (policy, f"'{field}' must be a list of names")
        cleaned: list[str] = []
        rejected: list[str] = []
        for item in raw:
            clean = _clean_dns_hostname(item)
            if clean is None:
                rejected.append(item if isinstance(item, str) else repr(item))
            elif clean not in cleaned:
                cleaned.append(clean)
        if rejected:
            return (policy, f"'{field}' contains values that are not names: {', '.join(rejected)}")
        policy[field] = cleaned
    raw_all = body.get("intercept_all")
    if raw_all is not None:
        if not isinstance(raw_all, bool):
            return (policy, "'intercept_all' must be a boolean")
        policy["intercept_all"] = raw_all
    return (policy, None)


def _apply_dns_settings(config: Config | None = None) -> None:
    """Push the interception policy into the running DNS interceptor.

    Called after the discovery API writes the settings -- and, as a config callback,
    when the file is edited by hand, so the panel and ``config.yaml`` mean the same
    thing. Either way the listening sockets stay up and the next query already
    follows the new rule. ``config`` is passed by the watcher; the API path reads the
    live one.
    """
    interceptor = get_dns_interceptor()
    if interceptor is None:
        return
    try:
        interceptor.apply_settings(
            config if config is not None else _server().config_manager.config
        )
    except Exception:
        logger.exception("DNS: could not apply the new interception settings")


@router.get("/api/dns/discovery")
async def dns_discovery(limit: int = 500):
    """What each client asks the DNS for, and what the proxy does with it now.

    This is the answer to "what should we intercept next?" read off real traffic:
    the names seen on the DNS port, including the ones still being forwarded.
    Counters not yet flushed to the database are merged in, so a query just made
    by a device already appears.
    """
    interceptor = get_dns_interceptor()
    bounded = max(1, min(int(limit), 5000))
    entries: dict[tuple[str, str, str], dict] = {}
    if _server().db_manager:
        for row in await _server().db_manager.list_dns_queries(bounded):
            entries[row["client_ip"], row["hostname"], row["qtype"]] = row
    recorder = get_dns_recorder()
    if recorder is not None:
        for item in recorder.snapshot():
            key = (item["client_ip"], item["hostname"], item["qtype"])
            fresh = {
                **item,
                "first_seen": item["first_seen"].isoformat(),
                "last_seen": item["last_seen"].isoformat(),
            }
            existing = entries.get(key)
            if existing is None:
                entries[key] = fresh
                continue
            existing["hit_count"] += fresh["hit_count"]
            existing["last_seen"] = max(existing["last_seen"] or "", fresh["last_seen"])
    for entry in entries.values():
        hostname = entry["hostname"]
        decision = interceptor.classify(hostname) if interceptor else "forwarded"
        entry["intercepted"] = decision == "intercepted"
        entry["excluded"] = decision == "excluded"
        entry["suggested"] = decision == "forwarded"
        entry["excluded_by"] = interceptor.matching_exclusion(hostname) if interceptor else None
        entry["claimed_by"] = interceptor.adapter_claim(hostname) if interceptor else None
    cfg = _server().config_manager.config.dns if _server().config_manager else None
    return {
        "enabled": bool(cfg.enabled) if cfg else False,
        "intercept": list(cfg.intercept or []) if cfg else [],
        "intercept_all": bool(cfg.intercept_all) if cfg else False,
        "exclude": list(cfg.exclude or []) if cfg else [],
        "listen": {"host": interceptor.host, "port": interceptor.port, **interceptor.listening}
        if interceptor
        else {},
        "stats": dict(interceptor.stats) if interceptor else {},
        "pending_pairs": recorder.pending_pairs if recorder else 0,
        "flushed_pairs": recorder.flushed_pairs if recorder else 0,
        "flush_errors": recorder.flush_errors if recorder else 0,
        "dropped_pairs": recorder.dropped_pairs if recorder else 0,
        "entries": sorted(entries.values(), key=lambda e: e["last_seen"] or "", reverse=True)[
            :bounded
        ],
    }


@router.post("/api/dns/intercept")
async def dns_add_intercept(request: Request):
    """Promote a discovered hostname (or pattern) to an intercepted name."""
    if not _server().config_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    hostname = _clean_dns_hostname(body.get("hostname") or body.get("name"))
    if hostname is None:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide a valid 'hostname' or pattern (e.g. *.example.com)"},
        )
    current = list(_server().config_manager.config.dns.intercept or [])
    if hostname in current:
        return {"hostname": hostname, "intercept": current, "changed": False}
    current.append(hostname)
    if not _server().config_manager.set_dns_settings(intercept=current):
        return JSONResponse(status_code=500, content={"error": "Failed to persist config"})
    _apply_dns_settings()
    return {"hostname": hostname, "intercept": current, "changed": True}


@router.delete("/api/dns/intercept/{hostname}")
async def dns_remove_intercept(hostname: str):
    """Stop intercepting a name: it goes back to being forwarded (and discovered)."""
    if not _server().config_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    clean = _clean_dns_hostname(hostname)
    if clean is None:
        return JSONResponse(status_code=400, content={"error": "Invalid hostname"})
    previous = list(_server().config_manager.config.dns.intercept or [])
    current = [pattern for pattern in previous if pattern.strip().lower().rstrip(".") != clean]
    if len(current) == len(previous):
        return {"hostname": clean, "intercept": current, "removed": 0}
    if not _server().config_manager.set_dns_settings(intercept=current):
        return JSONResponse(status_code=500, content={"error": "Failed to persist config"})
    _apply_dns_settings()
    return {"hostname": clean, "intercept": current, "removed": len(previous) - len(current)}


@router.get("/api/dns/settings")
async def dns_get_settings():
    """The whole DNS interception policy, as the UI needs to render it."""
    if not _server().config_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    cfg = _server().config_manager.config.dns
    interceptor = get_dns_interceptor()
    return {
        "enabled": bool(cfg.enabled),
        "listen_host": cfg.listen_host,
        "listen_port": cfg.listen_port,
        "answer_ip": cfg.answer_ip,
        "answer_ttl": cfg.answer_ttl,
        "dns_servers": [*cfg.dns_servers, *cfg.dns_servers_v6],
        "intercept": list(cfg.intercept or []),
        "intercept_all": bool(cfg.intercept_all),
        "exclude": list(cfg.exclude or []),
        "listen": {"host": interceptor.host, "port": interceptor.port, **interceptor.listening}
        if interceptor
        else {},
    }


@router.put("/api/dns/settings")
async def dns_put_settings(request: Request):
    """Update the interception policy: the name list, ``intercept_all``, exclusions.

    Only the fields present in the body are touched, so the UI can flip one
    switch without resending (and risking) the others.
    """
    if not _server().config_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON body: expected an object"}
        )
    policy, error = _dns_policy_from_body(body)
    if error:
        return JSONResponse(status_code=400, content={"error": error})
    if not _server().config_manager.set_dns_settings(**policy):
        return JSONResponse(status_code=500, content={"error": "Failed to persist config"})
    _apply_dns_settings()
    cfg = _server().config_manager.config.dns
    return {
        "intercept": list(cfg.intercept or []),
        "intercept_all": bool(cfg.intercept_all),
        "exclude": list(cfg.exclude or []),
    }


@router.delete("/api/dns/discovery")
async def dns_clear_discovery():
    """Forget the recorded queries (memory and database)."""
    recorder = get_dns_recorder()
    removed = await recorder.clear() if recorder is not None else 0
    if recorder is None and _server().db_manager:
        removed = await _server().db_manager.clear_dns_queries()
    return {"removed": removed}
