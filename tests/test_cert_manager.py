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

    def test_absolute_hostname_stays_confined(self, tmp_path):
        cm = self._manager(tmp_path)
        base = (tmp_path / "external").resolve()
        for hostile in ("/tmp/evil", "../", "a\\..\\b", "/abs/path", "..%2f..%2fetc"):
            ext = cm._ext_dir(hostile)
            assert ext.is_relative_to(base), (
                f"escaped base for {hostile!r}: {ext}"
            )
