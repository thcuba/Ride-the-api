"""Tests for Frida pinning bypass script generator (core.tls.frida)."""

from __future__ import annotations

from core.tls.frida import FRIDA_PINNING_BYPASS_SCRIPT, get_frida_script


def test_get_frida_script_returns_template():
    script = get_frida_script()
    assert script == FRIDA_PINNING_BYPASS_SCRIPT
    assert "Java.perform" in script
    assert "X509TrustManager" in script
    assert "checkServerTrusted" in script
    assert "HostnameVerifier" in script
    assert "CertificatePinner" in script
    assert "[RideTheAPI]" in script
