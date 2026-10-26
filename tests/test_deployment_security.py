"""Deployment defaults that protect the administrative control plane."""

from pathlib import Path

import yaml


def test_compose_publishes_http_control_plane_on_loopback_only():
    """The HTTP dashboard/API must not be exposed to the LAN by default."""
    compose_path = Path(__file__).parents[1] / "deploy" / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    ports = compose["services"]["ride-the-api"]["ports"]
    assert "127.0.0.1:8080:8911" in ports
