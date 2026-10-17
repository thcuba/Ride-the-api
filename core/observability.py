"""Observability: Prometheus metrics and OpenTelemetry tracing.

The config model (``MetricsConfig``/``TracingConfig``) has existed for a long
time, but until now nothing started the exporters. This module implements the
documented behaviour:

* ``observability.metrics``  -> a Prometheus ``/metrics`` endpoint on its own
  port (default ``9090``), named metrics ``ride_api_*`` plus the standard
  ``python_*`` runtime metrics.
* ``observability.tracing`` -> an OpenTelemetry ``Tracer`` (console or OTLP
  exporter) used to time the proxy request/response path.

The module is deliberately self-contained and never imports the ASGI app, so it
stays importable in PyInstaller bundles and test doubles.
"""

from __future__ import annotations

import functools
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class MetricsRegistry:
    """Owns the Prometheus collectors backing the ``ride_api_*`` metrics.

    Kept separate from the global ``REGISTRY`` so tests can build isolated
    instances and so nothing else (or a future default registry dump) pollutes
    the namespace.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests_total = Counter(
            "ride_api_requests_total",
            "Total requests processed",
            registry=self.registry,
        )
        self.local_hits_total = Counter(
            "ride_api_local_hits_total",
            "Local responses served",
            registry=self.registry,
        )
        self.cloud_misses_total = Counter(
            "ride_api_cloud_misses_total",
            "Requests forwarded to the cloud",
            registry=self.registry,
        )
        self.device_count = Gauge(
            "ride_api_device_count",
            "Registered devices",
            registry=self.registry,
        )
        self.buffer_size_bytes = Gauge(
            "ride_api_buffer_size_bytes",
            "Total buffer size in bytes",
            registry=self.registry,
        )

    def render(self) -> bytes:
        """Serialize the registry in Prometheus text exposition format."""
        return generate_latest(self.registry)


# A single shared instance to hook from the ASGI app without plumbing a
# reference through every route. Tests build their own `MetricsRegistry`.
registry = MetricsRegistry()


class _MetricsHTTPRequestHandler(BaseHTTPRequestHandler):
    """Serve the configured metrics path from the shared registry.

    ``path_override``/``on_scrape`` arrive via ``functools.partial`` so each
    server serves its own path/callback without process-global class state.
    """

    path_override: str = "/metrics"
    on_scrape: Callable[[], None] | None = None

    def __init__(
        self,
        request: object,
        client_address: tuple[str, int],
        server: ThreadingHTTPServer,
        path_override: str = "/metrics",
        on_scrape: Callable[[], None] | None = None,
    ) -> None:
        self.path_override = path_override
        self.on_scrape = on_scrape
        super().__init__(request, client_address, server)  # type: ignore[arg-type]

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path != self.path_override:
            self.send_error(404)
            return
        try:
            if self.on_scrape is not None:
                self.on_scrape()
            body = registry.render()
        except Exception:  # noqa: BLE001
            logger.exception("Metrics scrape failed")
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:  # noqa: A002
        # Scrapers hit /metrics every poll; keep the access log quiet.
        logger.debug("metrics access: " + message_format, *args)


class MetricsServer:
    """A tiny by-design HTTP server exposing the Prometheus endpoint.

    Lives on its own port (default 9090) per the documentation, separate from
    the proxy's 8911. The server is deliberately simple (no auth, no HTTPS) as
    documented; Prometheus scrapers are expected to be network-isolated.
    """

    def __init__(self, port: int, path: str, on_scrape: Callable[[], None] | None = None) -> None:
        self.port = port
        self.path = path
        self._thread: threading.Thread | None = None
        handler = functools.partial(
            _MetricsHTTPRequestHandler, path_override=path, on_scrape=on_scrape
        )
        self._server = ThreadingHTTPServer(("0.0.0.0", port), handler)

    def start(self) -> None:
        """Run the server on a background daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        if self._server is None:
            raise RuntimeError("Metrics server already stopped")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"ride-metrics-{self.port}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Prometheus metrics listening on 0.0.0.0:%s%s", self.port, self.path)

    def stop(self) -> None:
        """Shut the server down (best-effort, safe to call multiple times)."""
        if self._thread is not None and self._thread.is_alive():
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def start_metrics_server(
    port: int, path: str, on_scrape: Callable[[], None] | None = None
) -> MetricsServer:
    """Build and start the Prometheus endpoint server, returning its handle."""
    server = MetricsServer(port, path, on_scrape)
    server.start()
    return server


def build_tracer(exporter_name: str, otlp_endpoint: str, sample_rate: float = 1.0):
    """Construct an OpenTelemetry ``TracerProvider`` for a callable exporter.

    ``exporter`` in {``console``, ``otlp``}. ``otlp`` requires the optional
    ``opentelemetry-exporter-otlp`` package; if absent we fall back to console
    and log once so misconfiguration is diagnosable instead of silent.
    ``sample_rate`` (0.0–1.0) selects the probability a new trace is sampled.
    """
    from opentelemetry import trace as otel_trace  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased  # noqa: PLC0415

    sampler = TraceIdRatioBased(max(0.0, min(sample_rate, 1.0)))
    provider = TracerProvider(sampler=sampler)
    try:
        if exporter_name == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter,
            )

            span_exporter: object = OTLPSpanExporter(endpoint=otlp_endpoint)
        else:
            span_exporter = ConsoleSpanExporter()
    except ImportError:
        logger.warning(
            "opentelemetry-exporter-otlp is not installed; falling back to the "
            "console trace exporter. Install it to export traces via OTLP."
        )
        span_exporter = ConsoleSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))  # type: ignore[arg-type]
    otel_trace.set_tracer_provider(provider)
    return otel_trace.get_tracer("ride-the-api")


def get_tracer():
    """Return the app tracer, initializing the provider once if needed."""
    from opentelemetry import trace as otel_trace  # noqa: PLC0415

    return otel_trace.get_tracer("ride-the-api")
