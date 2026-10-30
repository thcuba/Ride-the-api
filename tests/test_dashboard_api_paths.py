"""Every API path the dashboard calls must exist as a route.

A route renamed or removed on the server leaves the panel fetching a 404, and that
failure is silent: the panel just shows "error" (or an empty list), no test fails,
and nobody notices until an operator says the button does nothing. This file reads
the paths straight out of `webui/dashboard.html` and matches them against the app's
real routes, so the drift surfaces here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import core.server as server_mod

DASHBOARD = Path(__file__).resolve().parents[1] / "webui" / "dashboard.html"

#: Stands in for a `${...}` template hole while matching route patterns.
_HOLE = "\x00"

_FETCH_CALL = re.compile(r"fetch\(\s*[`'\"]([^`'\"\n]+)")


def _dashboard_paths() -> list[str]:
    """API paths the dashboard fetches, with template holes normalised."""
    found = {
        re.sub(r"\$\{[^}]*\}", _HOLE, match.group(1)).split("?")[0]
        for match in _FETCH_CALL.finditer(DASHBOARD.read_text(encoding="utf-8"))
    }
    return sorted(path for path in found if path.startswith("/api/") or path == "/health")


def _route_patterns() -> list[re.Pattern[str]]:
    """One regex per app route, with `{param}` turned into a single path segment."""
    patterns = []
    for route in server_mod.app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path.startswith("/"):
            patterns.append(re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"))
    return patterns


def _literal_tails() -> set[str]:
    """Last path segments the API uses as fixed words (``start``, ``stop``, ...).

    A `${...}` hole is not always an id: `/api/protocol-servers/{name}/${action}`
    passes ``start`` or ``stop``. Those come from the route table rather than from a
    list kept here, so the check follows the API instead of drifting from it.
    """
    tails: set[str] = set()
    for route in server_mod.app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path.startswith("/"):
            tail = path.rsplit("/", 1)[-1]
            if tail and not tail.startswith("{") and "{" not in path.rsplit("/", 1)[0][-1:]:
                tails.add(tail)
    return tails


def _candidates(path: str) -> list[str]:
    """Concrete URLs a dashboard path can turn into at runtime."""
    probe = path.replace(_HOLE, "probe")
    if probe.endswith("/"):
        probe += "probe"
    candidates = [probe]
    if path.endswith(_HOLE) or path.endswith("/"):
        head = path.rsplit("/", 1)[0].replace(_HOLE, "probe")
        candidates += [f"{head}/{word}" for word in sorted(_literal_tails())]
    return candidates


def test_the_guard_reads_the_paths_it_thinks_it_reads():
    """A regex that matched nothing would make the check below pass vacuously."""
    paths = _dashboard_paths()

    assert len(paths) > 20  # noqa: PLR2004
    assert "/api/dns/discovery" in paths
    assert "/api/devices" in paths
    assert "start" in _literal_tails()
    assert "discovery" in _literal_tails()


@pytest.mark.parametrize("path", _dashboard_paths())
def test_every_path_the_dashboard_fetches_is_routed(path):
    patterns = _route_patterns()

    assert any(
        pattern.match(candidate) for candidate in _candidates(path) for pattern in patterns
    ), f"the dashboard fetches {path!r} but no route matches it"
