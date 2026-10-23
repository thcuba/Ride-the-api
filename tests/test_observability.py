"""Tests for core.observability (Prometheus metrics + tracing hooks)."""

from __future__ import annotations

import re
import socket
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from core import observability as obs

_OK = 200
_NOT_FOUND = 404
_SERVER_ERROR = 500


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_registry_render_contains_named_metrics() -> None:
    reg = obs.MetricsRegistry()
    body = reg.render().decode()
    assert "ride_api_requests_total" in body
    assert "ride_api_local_hits_total" in body
    assert "ride_api_cloud_misses_total" in body
    assert "ride_api_device_count" in body
    assert "ride_api_buffer_size_bytes" in body


def test_registry_counters_increment_reflect_in_render() -> None:
    reg = obs.MetricsRegistry()
    reg.requests_total.inc()
    reg.requests_total.inc()
    reg.local_hits_total.inc()
    reg.cloud_misses_total.inc()
    body = reg.render().decode()
    # Counter samples appear as "<name> <value>"; the -created metadata is a
    # separate gauge line we do not assert on.

    def val(name: str) -> float:
        m = re.search(rf"^{name} ([\d.]+)$", body, re.MULTILINE)
        assert m, f"{name} not found in render"
        return float(m.group(1))

    assert val("ride_api_requests_total") == 2.0  # noqa: PLR2004
    assert val("ride_api_local_hits_total") == 1.0  # noqa: PLR2004
    assert val("ride_api_cloud_misses_total") == 1.0  # noqa: PLR2004


def test_registry_gauge_set_reflect() -> None:
    reg = obs.MetricsRegistry()
    reg.device_count.set(3)
    reg.buffer_size_bytes.set(1024)
    body = reg.render().decode()
    assert "\nride_api_device_count 3.0\n" in body
    assert "\nride_api_buffer_size_bytes 1024.0\n" in body


def test_metrics_server_serves_metrics_and_404_elsewhere() -> None:
    served_paths: list[str] = []

    def on_scrape() -> None:
        served_paths.append("scrape")
        obs.registry.device_count.set(7)

    port = _free_port()
    server = obs.MetricsServer(port, "/metrics", on_scrape=on_scrape)
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            assert resp.status == _OK
            content_type = resp.headers["Content-Type"]
            assert content_type.startswith("text/plain")
            body = resp.read().decode()
        assert "ride_api_requests_total" in body
        assert "ride_api_device_count 7.0" in body
        assert served_paths, "on_scrape callback was never invoked"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5)
        assert exc.value.code == _NOT_FOUND
    finally:
        server.stop()


def test_metrics_server_stop_is_idempotent() -> None:
    port = _free_port()
    server = obs.MetricsServer(port, "/metrics")
    server.start()
    assert server.is_running
    server.stop()
    assert not server.is_running
    server.stop()  # second call must not raise


def test_metrics_server_handles_render_exception() -> None:
    port = _free_port()
    server = obs.MetricsServer(port, "/metrics")
    server.start()
    try:
        with patch.object(obs.registry, "render", side_effect=Exception("Render error")):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5)
            assert exc.value.code == _SERVER_ERROR
    finally:
        server.stop()


def test_metrics_server_handles_on_scrape_exception() -> None:
    def bad_on_scrape() -> None:
        raise RuntimeError("Callback error")

    port = _free_port()
    server = obs.MetricsServer(port, "/metrics", on_scrape=bad_on_scrape)
    server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert exc.value.code == _SERVER_ERROR
    finally:
        server.stop()


def test_build_tracer_uses_console_by_default():
    tracer = obs.build_tracer("console", "http://localhost:4317")
    assert tracer is not None


def test_metrics_registry_isolated_from_global() -> None:
    """Building a MetricsRegistry should not touch the shared one."""
    before = obs.registry.registry.get_sample_value("ride_api_requests_total")
    r = obs.MetricsRegistry()
    r.requests_total.inc()
    after = obs.registry.registry.get_sample_value("ride_api_requests_total")
    assert before == after  # global untouched by isolated counter
    # and the isolated registry saw our increment
    assert re.search(r"^ride_api_requests_total 1.0$", r.render().decode(), re.MULTILINE)
