"""
Certificate Manager — Auto-generates root CA + per-hostname leaf certificates.

On first run, creates a root CA (RSA 4096, SHA-256) in ./certs/.
For every new hostname (SNI) seen during TLS interception, generates
a leaf certificate signed by the CA and caches it on disk.

No user interaction required for generation. The CA cert can be downloaded
via API for manual installation on devices.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)


class CertManager:
    """Manages CA and per-hostname leaf certificates for TLS interception."""

    def __init__(  # noqa: PLR0913
        self,
        ca_cert_path: str = "./certs/ca.pem",
        ca_key_path: str = "./certs/ca.key",
        device_certs_dir: str = "./data/device_certs",
        external_certs_dir: str = "./data/external_certs",
        ca_key_size: int = 4096,
        leaf_key_size: int = 2048,
        cert_validity_days: int = 730,  # 2 years
    ) -> None:
        self.ca_cert_path = Path(ca_cert_path)
        self.ca_key_path = Path(ca_key_path)
        self.device_certs_dir = Path(device_certs_dir)
        self.external_certs_dir = Path(external_certs_dir)
        self.ca_key_size = ca_key_size
        self.leaf_key_size = leaf_key_size
        self.cert_validity_days = cert_validity_days

        self._ca_cert: x509.Certificate | None = None
        self._ca_key: rsa.RSAPrivateKey | None = None
        self._cache: dict[str, tuple[str, str]] = {}  # hostname -> (cert_pem, key_pem)

        # Ensure directories exist
        self.ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
        self.device_certs_dir.mkdir(parents=True, exist_ok=True)
        self.external_certs_dir.mkdir(parents=True, exist_ok=True)

    # ── CA Management ──────────────────────────────────────────────────────────

    def ensure_ca(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """Load or generate the root CA certificate and key."""
        if self._ca_cert is not None and self._ca_key is not None:
            return self._ca_cert, self._ca_key

        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            self._load_ca()
        else:
            self._generate_ca()

        return self._ca_cert, self._ca_key  # type: ignore[return-value]

    def _load_ca(self) -> None:
        """Load existing CA from disk."""
        try:
            with self.ca_key_path.open("rb") as f:
                self._ca_key = serialization.load_pem_private_key(f.read(), password=None)
            with self.ca_cert_path.open("rb") as f:
                self._ca_cert = x509.load_pem_x509_certificate(f.read())
            logger.info("Loaded existing CA from %s", self.ca_cert_path)
        except Exception as e:
            logger.warning("Failed to load CA (%s), re-generating", e)
            self._generate_ca()

    def _generate_ca(self) -> None:
        """Generate a new root CA certificate and key."""
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.ca_key_size,
        )
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "Ride the API Local CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ride the API"),
                x509.NameAttribute(NameOID.COUNTRY_NAME, "XX"),
            ]
        )

        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=self.cert_validity_days * 5))  # CA is long-lived
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    key_cert_sign=True,
                    crl_sign=True,
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        self._ca_key = key
        self._ca_cert = cert

        self.ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ca_key_path.open("wb") as f:
            f.write(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
        # Root CA private key must not be world-readable — a 0644 default
        # umask exposes it to any local user. Restrict to owner read/write.
        self.ca_key_path.chmod(0o600)
        with self.ca_cert_path.open("wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        logger.info("Generated new root CA at %s", self.ca_cert_path)

    def ca_cert_pem(self) -> str:
        """Return the CA certificate as a PEM string."""
        ca_cert, _ = self.ensure_ca()
        return ca_cert.public_bytes(serialization.Encoding.PEM).decode()

    # ── Leaf Certificate Management ────────────────────────────────────────────

    def get_cert_for_hostname(self, hostname: str) -> tuple[str, str]:
        """Get (cert_pem, key_pem) for a hostname, generating + caching if needed."""
        self._validate_hostname(hostname)
        # Check in-memory cache
        if hostname in self._cache:
            return self._cache[hostname]

        # Check external cert first (higher priority)
        ext = self._load_external_cert(hostname)
        if ext:
            cert_pem, key_pem = ext
            self._cache[hostname] = (cert_pem, key_pem)
            return cert_pem, key_pem

        # Check disk cache
        cert_file, key_file = self._device_cert_files(hostname)

        if cert_file.exists() and key_file.exists():
            cert_pem = cert_file.read_text()
            key_pem = key_file.read_text()
            self._cache[hostname] = (cert_pem, key_pem)
            return cert_pem, key_pem

        # Generate new leaf certificate
        cert_pem, key_pem = self._generate_leaf_cert(hostname)

        # Save to disk cache
        cert_file.write_text(cert_pem)
        key_file.write_text(key_pem)
        # Private key must not be world-readable.
        key_file.chmod(0o600)
        self._cache[hostname] = (cert_pem, key_pem)
        logger.debug("Generated leaf certificate for %s", hostname)

        return cert_pem, key_pem

    def _generate_leaf_cert(self, hostname: str) -> tuple[str, str]:
        """Generate a leaf certificate signed by the CA for the given hostname."""
        ca_cert, ca_key = self.ensure_ca()

        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.leaf_key_size,
        )

        now = datetime.now(UTC)

        # Build subject/issuer
        try:
            ipaddress.ip_address(hostname)
            is_ip = True
        except ValueError:
            is_ip = False

        if is_ip:
            san = [x509.IPAddress(ipaddress.ip_address(hostname))]
            subject_name = x509.Name(
                [
                    x509.NameAttribute(NameOID.COMMON_NAME, hostname),
                ]
            )
        else:
            san = [x509.DNSName(hostname)]
            # Try to add a wildcard for the domain
            if "." in hostname:
                parts = hostname.split(".")
                if len(parts) >= 2:  # noqa: PLR2004
                    san.append(x509.DNSName(f"*.{'.'.join(parts[1:])}"))
            subject_name = x509.Name(
                [
                    x509.NameAttribute(NameOID.COMMON_NAME, hostname),
                ]
            )

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject_name)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=self.cert_validity_days))
            .add_extension(
                x509.SubjectAlternativeName(san),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    content_commitment=False,
                    data_encipherment=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        return cert_pem, key_pem

    # ── Utilities ──────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return certificate statistics."""
        cert_count = 0
        if self.device_certs_dir.exists():
            cert_count = len(list(self.device_certs_dir.glob("*.pem")))

        ext_count = 0
        ext_meta = self._external_certs_meta()
        if ext_meta:
            ext_count = len(ext_meta)

        return {
            "ca_exists": self.ca_cert_path.exists(),
            "leaf_certs_cached": cert_count,
            "in_memory_cache": len(self._cache),
            "external_certs": ext_count,
        }

    # ── External Certificate Import ───────────────────────────────────────────

    def import_cert(self, hostname: str, cert_pem: str, key_pem: str, label: str = "") -> dict:
        """Import an external certificate for a hostname.

        Stores in data/external_certs/{hostname}/ and adds metadata.
        Once imported, get_cert_for_hostname() will return the external cert.
        """
        self._validate_hostname(hostname)
        ext_dir = self._ext_dir(hostname)
        ext_dir.mkdir(parents=True, exist_ok=True)

        cert_path = ext_dir / "cert.pem"
        key_path = ext_dir / "key.pem"
        meta_path = ext_dir / "meta.json"

        cert_path.write_text(cert_pem)
        key_path.write_text(key_pem)
        # Private key must not be world-readable.
        key_path.chmod(0o600)

        # Parse the imported cert to extract metadata
        cert_info = self._parse_cert_info(cert_pem)

        meta = {
            "hostname": hostname,
            "label": label,
            "imported_at": datetime.now(UTC).isoformat(),
            "type": "imported",
            "issuer": cert_info.get("issuer", ""),
            "subject": cert_info.get("subject", ""),
            "not_before": cert_info.get("not_before", ""),
            "not_after": cert_info.get("not_after", ""),
            "sans": cert_info.get("sans", []),
        }

        with meta_path.open("w") as f:
            json.dump(meta, f, indent=2)

        # Update in-memory cache so TLS MITM picks it up immediately
        self._cache[hostname] = (cert_pem, key_pem)

        logger.info(
            "Imported external certificate for %s (expires: %s)", hostname, meta["not_after"]
        )
        return meta

    def delete_cert(self, hostname: str) -> bool:
        """Delete an imported external certificate. Falls back to auto-generated."""
        self._validate_hostname(hostname)
        ext_dir = self._ext_dir(hostname)
        if not ext_dir.exists():
            return False

        shutil.rmtree(str(ext_dir))

        # Clear cache so next call regenerates
        self._cache.pop(hostname, None)

        logger.info("Deleted external certificate for %s", hostname)
        return True

    def list_imported_certs(self) -> list[dict]:
        """List all imported external certificates with metadata."""
        return self._external_certs_meta()

    def get_cert_info(self, hostname: str) -> dict | None:
        """Get metadata for a specific external certificate."""
        self._validate_hostname(hostname)
        meta_path = self._ext_dir(hostname) / "meta.json"
        if meta_path.exists():
            try:
                with meta_path.open() as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to read cert meta for %s: %s", hostname, e)
        return None

    def has_external_cert(self, hostname: str) -> bool:
        """Check if an external certificate exists for hostname."""
        self._validate_hostname(hostname)
        return (self._ext_dir(hostname) / "cert.pem").exists()

    def _ext_dir(self, hostname: str) -> Path:
        """Get path to external cert directory for hostname.

        Path is confined to the base external-certs directory. Even though
        ``_safe_filename`` strips path separators, an explicit containment
        guard is applied so a malformed hostname can never produce a path
        outside the base (defence in depth against traversal, incl. abs-path
        and ``..`` escape).
        """
        base = getattr(self, "external_certs_dir", None)
        if base is None:
            base = Path("./data/external_certs")
        base = Path(base).resolve()
        self._validate_hostname(hostname)
        ext = (base / self._safe_filename(hostname)).resolve()
        if base != ext and base not in ext.parents:
            raise ValueError(f"Unsafe hostname path: {hostname!r}")
        return ext

    _HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

    def _validate_hostname(self, hostname: str) -> str:
        """Return a validated hostname safe to embed in a cert path.

        Accepts DNS-style names (letters, digits, dots, hyphens, underscores,
        optional single trailing dot). Rejects anything with path separators
        (``/``, ``\\\\``), NUL, whitespace or other metacharacters. This
        allowlist check runs before any path is built, so the derived cert
        path cannot escape the certs directory.
        """
        if not hostname:
            raise ValueError("Empty hostname")
        if not self._HOSTNAME_RE.fullmatch(hostname):
            raise ValueError(f"Unsafe hostname: {hostname!r}")
        return hostname

    def _device_cert_files(self, hostname: str) -> tuple[Path, Path]:
        """Return (cert, key) paths under the device certs dir for hostname."""
        base = self.device_certs_dir.resolve()
        self._validate_hostname(hostname)
        safe = self._safe_filename(hostname)
        cert = (base / f"{safe}.pem").resolve()
        key = (base / f"{safe}.key").resolve()
        for p in (cert, key):
            if base != p and base not in p.parents:
                raise ValueError(f"Unsafe hostname path: {hostname!r}")
        return cert, key

    def _load_external_cert(self, hostname: str) -> tuple[str, str] | None:
        """Load external cert and key from disk if they exist."""
        ext_dir = self._ext_dir(hostname)
        cert_path = ext_dir / "cert.pem"
        key_path = ext_dir / "key.pem"
        if cert_path.exists() and key_path.exists():
            return cert_path.read_text(), key_path.read_text()
        return None

    def _external_certs_meta(self) -> list[dict]:
        """Scan external certs directory and collect metadata."""
        base = getattr(self, "external_certs_dir", None)
        if base is None:
            base = Path("./data/external_certs")
        base = Path(base)
        if not base.exists():
            return []

        certs = []
        for meta_file in base.glob("*/meta.json"):
            try:
                with meta_file.open() as f:
                    certs.append(json.load(f))
            except Exception as e:
                logger.warning("Failed to read %s: %s", meta_file, e)
        return certs

    @staticmethod
    def _parse_cert_info(cert_pem: str) -> dict:
        """Extract metadata from a PEM certificate string."""
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode())
            info = {
                "issuer": cert.issuer.rfc4514_string() if cert.issuer else "",
                "subject": cert.subject.rfc4514_string() if cert.subject else "",
                "not_before": cert.not_valid_before_utc.isoformat()
                if hasattr(cert, "not_valid_before_utc")
                else "",
                "not_after": cert.not_valid_after_utc.isoformat()
                if hasattr(cert, "not_valid_after_utc")
                else "",
                "sans": [],
            }
            try:
                ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                info["sans"] = [str(s) for s in ext.value]
            except x509.ExtensionNotFound:
                pass
        except Exception as e:
            logger.warning("Failed to parse cert: %s", e)
            return {}
        else:
            return info

    @staticmethod
    def _safe_filename(hostname: str) -> str:
        """Convert a hostname to a safe filename.

        Sanitizes path separators (/ and \\) too — otherwise a hostname such as
        ``/tmp/evil`` (or ``..`` escaped) becomes an absolute/escaping path when
        joined with ``Path(base)``, allowing arbitrary file writes and
        ``shutil.rmtree`` on attacker-chosen directories (e.g. via the
        delete_cert / get_cert_for_hostname / import paths).
        """
        return (
            hostname.replace("*", "wildcard_")
            .replace(".", "_")
            .replace(":", "_")
            .replace("/", "_")
            .replace("\\", "_")
        )


# ── Global instance ─────────────────────────────────────────────────────────

_cert_manager: CertManager | None = None


def get_cert_manager(
    ca_cert_path: str | None = None,
    ca_key_path: str | None = None,
    device_certs_dir: str | None = None,
    external_certs_dir: str | None = None,
) -> CertManager:
    """Get or create the global CertManager instance."""
    global _cert_manager  # noqa: PLW0603
    if _cert_manager is None:
        _cert_manager = CertManager(
            ca_cert_path=ca_cert_path or "./certs/ca.pem",
            ca_key_path=ca_key_path or "./certs/ca.key",
            device_certs_dir=device_certs_dir or "./data/device_certs",
            external_certs_dir=external_certs_dir or "./data/external_certs",
        )
    return _cert_manager
