"""Tests for server CORS security configuration."""

from http import HTTPStatus

from fastapi.testclient import TestClient

from core.server import app


def test_cors_middleware_allowed_origins():
    """Verify CORS middleware permits requests from trusted local origins."""
    client = TestClient(app)

    # Allowed origin 1: localhost
    response_lh = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:8911",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response_lh.status_code == HTTPStatus.OK
    assert response_lh.headers.get("access-control-allow-origin") == "http://localhost:8911"

    # Allowed origin 2: 127.0.0.1
    response_ip = client.options(
        "/health",
        headers={
            "Origin": "http://127.0.0.1:8911",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response_ip.status_code == HTTPStatus.OK
    assert response_ip.headers.get("access-control-allow-origin") == "http://127.0.0.1:8911"


def test_cors_middleware_disallowed_origin():
    """Verify CORS middleware rejects preflight requests from untrusted origins."""
    client = TestClient(app)
    response = client.options(
        "/health",
        headers={
            "Origin": "https://attacker.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette CORSMiddleware responds with 400 Bad Request for disallowed preflight origins
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.headers.get("access-control-allow-origin") is None
