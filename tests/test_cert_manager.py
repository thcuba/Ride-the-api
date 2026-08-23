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
            assert resolved.is_relative_to(base.resolve()), (
                f"escaped base for {hostile!r}: {sub}"
            )

    def test_wildcards_still_supported(self):
        """Normal hostname characters are preserved as before."""
        assert CertManager._safe_filename("api.example.com") == "api_example_com"
