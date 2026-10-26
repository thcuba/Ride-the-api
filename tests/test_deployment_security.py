"""Deployment defaults that protect the administrative control plane."""

from pathlib import Path

import yaml


def test_compose_publishes_http_control_plane_on_loopback_only():
    """The HTTP dashboard/API must not be exposed to the LAN by default."""
    compose_path = Path(__file__).parents[1] / "deploy" / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    ports = compose["services"]["ride-the-api"]["ports"]
    assert "127.0.0.1:8080:8911" in ports


def test_compose_config_mount_is_writable_for_password_setup():
    """First-run auth and saved settings must persist through the host bind mount."""
    compose_path = Path(__file__).parents[1] / "deploy" / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    service = compose["services"]["ride-the-api"]
    assert service["user"] == "${RTA_UID:-1000}:${RTA_GID:-1000}"
    assert "../config:/app/config" in service["volumes"]


def test_compose_trusts_forwarded_headers_only_from_nginx():
    """The API must recover client IPs without trusting arbitrary containers."""
    compose_path = Path(__file__).parents[1] / "deploy" / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    nginx_network = compose["services"]["nginx"]["networks"]["ride-the-api-net"]
    api_environment = compose["services"]["ride-the-api"]["environment"]
    assert nginx_network["ipv4_address"] == "${RTA_NGINX_IP:-172.30.0.2}"
    assert "FORWARDED_ALLOW_IPS=${RTA_NGINX_IP:-172.30.0.2}" in api_environment
    assert compose["networks"]["ride-the-api-net"]["ipam"]["config"][0]["subnet"] == (
        "${RTA_DOCKER_SUBNET:-172.30.0.0/24}"
    )
