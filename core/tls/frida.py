"""
Frida script generation for certificate pinning bypass.

Provides a dynamic instrumentation script to hook common TLS validation routines
on devices (e.g. Android/Java X509TrustManager, OkHttp CertificatePinner) so they
trust the local proxy CA.
"""

from __future__ import annotations

FRIDA_PINNING_BYPASS_SCRIPT: str = r"""// Ride the API — Certificate Pinning Bypass (Frida)
// Usage: frida -U -l script.js <app>
// Requires: Frida installed on device, CA cert installed

Java.perform(function() {
    // Common SSL pinning bypass targets
    var X509TrustManager = Java.use('javax.net.ssl.X509TrustManager');
    var SSLContext = Java.use('javax.net.ssl.SSLContext');

    // Bypass checkClientTrusted / checkServerTrusted
    X509TrustManager.checkClientTrusted.implementation = function(chain, authType) {
        console.log('[RideTheAPI] Bypassing checkClientTrusted');
    };
    X509TrustManager.checkServerTrusted.implementation = function(chain, authType) {
        console.log('[RideTheAPI] Bypassing checkServerTrusted');
    };
    X509TrustManager.getAcceptedIssuers.implementation = function() {
        console.log('[RideTheAPI] Returning empty accepted issuers');
        return [];
    };

    // Hook OkHttp HostnameVerifier
    try {
        var HostnameVerifier = Java.use('javax.net.ssl.HostnameVerifier');
        HostnameVerifier.verify.implementation = function(hostname, session) {
            console.log('[RideTheAPI] Bypassing hostname verification for: ' + hostname);
            return true;
        };
    } catch(e) { console.log('[RideTheAPI] HostnameVerifier not found'); }

    // Hook OkHttp CertificatePinner
    try {
        var CertificatePinner = Java.use('okhttp3.CertificatePinner');
        CertificatePinner.check.implementation = function(pin) {
            console.log('[RideTheAPI] Bypassing certificate pin for: ' + pin);
        };
    } catch(e) { console.log("[RideTheAPI] CertificatePinner not found"); }

    console.log("[RideTheAPI] Pinning bypass injected successfully");
});
"""


def get_frida_script() -> str:
    """Return the Frida JavaScript payload used to bypass certificate pinning."""
    return FRIDA_PINNING_BYPASS_SCRIPT
