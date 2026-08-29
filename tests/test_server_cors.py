"""Tests for server CORS security configuration."""

from http import HTTPStatus

from fastapi.testclient import TestClient

from core.server import app


def test_cors_middleware_configuration():
    """Verify CORS middleware is configured with allow_credentials=False for wildcard origins."""
    client = TestClient(app)
    # Send preflight OPTIONS request with Origin header
    response = client.options(
        "/health",
        headers={
            "Origin": "https://attacker.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == HTTPStatus.OK
    assert response.headers.get("access-control-allow-origin") == "*"
    # Access-Control-Allow-Credentials must NOT be true when allow_origin is *
    assert response.headers.get("access-control-allow-credentials") is None
