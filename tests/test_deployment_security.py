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


def test_nginx_verifies_the_real_cloud_it_forwards_to() -> None:
    """Forwards to the vendor cloud must check its certificate (S-13).

    nginx does not verify upstream certificates by default, so anyone on the
    upstream path could answer as the vendor.
    """
    from pathlib import Path  # noqa: PLC0415

    for name in ("nginx.conf", "nginx.conf.template"):
        text = (Path(__file__).resolve().parents[1] / "deploy" / name).read_text()
        upstream_tls_blocks = text.count("proxy_ssl_server_name on;")
        assert upstream_tls_blocks == 3, name  # two HTTP forwards, one MQTT stream
        assert text.count("proxy_ssl_verify on;") == upstream_tls_blocks, name
        assert text.count("proxy_ssl_trusted_certificate") == upstream_tls_blocks, name
        assert "proxy_ssl_name mqtt.example.com;" in text, name
