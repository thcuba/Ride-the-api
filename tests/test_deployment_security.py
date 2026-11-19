"""Deployment defaults that protect the administrative control plane."""

import re
from pathlib import Path

import yaml

# Upstream TLS connections in the nginx configs: two HTTP forwards, one MQTT stream.
_UPSTREAM_TLS_BLOCKS = 3


def _compose() -> dict:
    """The compose file, parsed."""
    path = Path(__file__).parents[1] / "deploy" / "docker-compose.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _nginx_mounted_cert_files() -> set[str]:
    """Basenames of the files the nginx container gets under /etc/nginx/certs."""
    prefix = "/etc/nginx/certs/"
    volumes = _compose()["services"]["nginx"]["volumes"]
    mounted = set()
    for volume in volumes:
        _host, _, target = volume.partition(":")
        container_path = target.split(":")[0]
        if container_path.startswith(prefix):
            mounted.add(container_path.rsplit("/", 1)[-1])
    return mounted


def test_nginx_cannot_read_the_ca_private_key() -> None:
    """The proxy container must not see certs/ca.key.

    The CA key sits next to the proxy certificate in certs/, so mounting the directory handed it to
    nginx: a compromise of that container was equivalent to holding the CA and being able to mint
    valid certificates for any host.
    """
    volumes = _compose()["services"]["nginx"]["volumes"]

    assert "../certs:/etc/nginx/certs:ro" not in volumes, (
        "il container nginx monta l'intera cartella certs"
    )
    assert not any("ca.key" in volume for volume in volumes), (
        "la chiave privata della CA non deve finire nel container nginx"
    )


def test_nginx_mounts_exactly_the_certificates_it_loads() -> None:
    """Every /etc/nginx/certs file the template loads must be mounted, and nothing else.

    Mounting the wrong names is not a cosmetic mistake: nginx refuses to start if it cannot read its
    certificate, so the mount list has to follow the template. This test is what keeps them in step.
    """
    root = Path(__file__).parents[1]
    template = (root / "deploy" / "nginx.conf.template").read_text(encoding="utf-8")
    needed = set(re.findall(r"/etc/nginx/certs/([A-Za-z0-9._-]+)", template))
    assert needed, "il template non carica alcun file da /etc/nginx/certs"

    mounted = _nginx_mounted_cert_files()
    assert needed <= mounted, f"nginx carica {sorted(needed - mounted)}, che non e' montato"
    assert mounted <= needed, f"montato ma non usato: {sorted(mounted - needed)}"


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
    for name in ("nginx.conf", "nginx.conf.template"):
        text = (Path(__file__).resolve().parents[1] / "deploy" / name).read_text()
        upstream_tls_blocks = text.count("proxy_ssl_server_name on;")
        assert upstream_tls_blocks == _UPSTREAM_TLS_BLOCKS, name
        assert text.count("proxy_ssl_verify on;") == upstream_tls_blocks, name
        assert text.count("proxy_ssl_trusted_certificate") == upstream_tls_blocks, name
        assert "proxy_ssl_name mqtt.example.com;" in text, name
