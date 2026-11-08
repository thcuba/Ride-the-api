"""``python -m core.server`` must hand the routers ONE live runtime module.

Regression guard for the split of ``core/server.py`` into ``core/routes/`` APIRouters:
``python -m`` executes the file as ``__main__``, so ``sys.modules`` has no
``core.server`` entry and the lazy ``from core import server`` in the routers
(needed to break the import cycle) imported a SECOND copy of the module. The
service logged a clean startup while every route reached through that accessor
answered ``503 {"error": "Service not ready"}`` — no devices, no LLM profiles, no
TLS certs in the dashboard, because the second copy's globals were all ``None``.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
import uvicorn

SERVER_FILE = (Path(__file__).resolve().parents[1] / "core" / "server.py").resolve()


@pytest.fixture
def run_entrypoint(monkeypatch):
    """Run ``core/server.py`` the way the ``-m`` entrypoint does, without serving.

    uvicorn is stubbed so the run stops at the last line of ``main()``; the stub
    records the module identity visible from inside the running process, which is
    exactly what a router's lazy import sees.
    """
    seen: dict[str, object] = {}

    def fake_uvicorn_run(*_args, **_kwargs):
        seen["main_module"] = sys.modules.get("__main__")
        seen["canonical_module"] = sys.modules.get("core.server")

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr("core.logging_config.setup_logging", lambda **_kwargs: None)

    saved_module = sys.modules.pop("core.server", None)
    core_package = sys.modules["core"]
    saved_attr = core_package.__dict__.pop("server", None)

    def run() -> dict[str, object]:
        runpy.run_module("core.server", run_name="__main__", alter_sys=True)
        return seen

    yield run

    if saved_module is not None:
        sys.modules["core.server"] = saved_module
    else:  # pragma: no cover - defensive restore
        sys.modules.pop("core.server", None)
    if saved_attr is not None:
        core_package.server = saved_attr
    else:  # pragma: no cover - defensive restore
        core_package.__dict__.pop("server", None)


def test_entrypoint_publishes_itself_as_the_canonical_module(run_entrypoint):
    seen = run_entrypoint()

    canonical = seen["canonical_module"]
    assert canonical is seen["main_module"], (
        "the -m entrypoint did not alias itself as core.server: the routers would "
        "import a second copy whose runtime state is all None (503 everywhere)"
    )
    assert Path(canonical.__file__).resolve() == SERVER_FILE


def test_router_accessor_resolves_to_the_running_entrypoint(run_entrypoint):
    seen = run_entrypoint()

    from core.routes.devices import _server  # noqa: PLC0415 - after the -m style run

    assert _server() is seen["main_module"], (
        "a router's lazy `from core import server` did not resolve to the running "
        "entrypoint module: everything routed through it would answer 503"
    )
