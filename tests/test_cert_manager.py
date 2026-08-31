"""Tests for TLS certificate manager security fixes.

Covers the path-traversal sanitization in ``CertManager._safe_filename``
(regression for the case where a hostname containing ``/`` or ``\\`` could
escape the external-certs directory and reach arbitrary paths).
"""

from pathlib import Path

from core.cert_manager import CertManager


class TestSafeFilename:
    def test_safe_filename_sanitizes_path_separators(self):
        """A hostname with '/' or '\\' must not survive as a path separator.

        Previously ``_safe_filename`` stripped only ``*``, ``.`` and ``:``, so a
        hostname like ``/tmp/evil`` (absolute) or ``../evil`` was joined with
        ``Path(base)`` and escaped the base directory — enabling arbitrary
        writes and ``shutil.rmtree`` on attacker-chosen paths.
        """
        for hostile in (
            "/tmp/evil",
            "../",
            "..%2fevil",
            "/abs/path",
            "data\\..\\..\\etc",
            "a/b/c",
        ):
            result = CertManager._safe_filename(hostname=hostile)
            # The result must be a plain relative filename with no separators.
            assert "/" not in result, f"slash leaked for {hostile!r}: {result!r}"
            assert "\\" not in result, f"backslash leaked for {hostile!r}: {result!r}"

    def test_safe_filename_stays_inside_base_dir(self, tmp_path):
        """Joining the sanitized name to a base dir never escapes it."""
        base = Path(tmp_path)
        for hostile in ("/tmp/evil", "../../../etc", "a\\..\\b"):
            sub = base / CertManager._safe_filename(hostname=hostile)
            # Resolve and confirm it is strictly inside the base.
            try:
                resolved = sub.resolve()
            except OSError:
                continue  # resolved may fail on missing parents; fine.
            assert resolved.is_relative_to(base.resolve()), f"escaped base for {hostile!r}: {sub}"

    def test_wildcards_still_supported(self):
        """DNS-safe hostname characters are preserved."""
        assert CertManager._safe_filename("api.example.com") == "api.example.com"

    def test_path_separators_are_stripped_to_single_segment(self):
        """A hostile hostname must fold to one safe filename segment."""
        assert "/" not in CertManager._safe_filename("/tmp/evil")
        assert "\\" not in CertManager._safe_filename("a\\..\\b")
        assert ".." not in CertManager._safe_filename("../")
        assert CertManager._safe_filename("").startswith("unknown")


class TestExtDirContainment:
    """The _ext_dir guard confines hostname-derived paths to the base dir."""

    def _manager(self, tmp_path):
        cm = object.__new__(CertManager)
        cm.external_certs_dir = tmp_path / "external"
        cm.external_certs_dir.mkdir(parents=True, exist_ok=True)
        cm.device_certs_dir = tmp_path / "device"
        cm.device_certs_dir.mkdir(parents=True, exist_ok=True)
        return cm

    def test_normal_hostname_stays_inside(self, tmp_path):
        cm = self._manager(tmp_path)
        ext = cm._ext_dir("api.example.com")
        assert ext.is_relative_to((tmp_path / "external").resolve())

    def test_hostile_hostname_rejected(self, tmp_path):
        """Hostnames with path separators are rejected before any path is built."""
        cm = self._manager(tmp_path)
        for hostile in ("/tmp/evil", "../", "a\\..\\b", "/abs/path", "..%2f..%2fetc"):
            try:
                cm._ext_dir(hostile)
                rejected = False
            except ValueError:
                rejected = True
            except OSError:
                rejected = True
            assert rejected, f"hostile hostname was accepted: {hostile!r}"


class TestCertKeyMatch:
    """The _cert_matches_key guard rejects mismatched cert/key pairs."""

    def _make_pair(self):
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.example")])
        for _ in range(5):  # FIPS/OpenSSL may need a retry when timing is unlucky
            builder = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=30))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            )
            try:
                cert = builder.sign(private_key=key, algorithm=hashes.SHA256())
                break
            except ValueError:
                continue
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()
        return cert_pem, key_pem

    def test_matching_cert_key_accepted(self):
        cert_pem, key_pem = self._make_pair()
        assert CertManager._cert_matches_key(cert_pem, key_pem) is True

    def test_mismatched_key_rejected(self):
        cert_pem, _ = self._make_pair()
        _, other_key = self._make_pair()
        assert CertManager._cert_matches_key(cert_pem, other_key) is False

    def test_garbage_pem_returns_false(self):
        assert CertManager._cert_matches_key("not a pem", "still not a key") is False
