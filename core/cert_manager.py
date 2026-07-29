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
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)


class CertManager:
    """Manages CA and per-hostname leaf certificates for TLS interception."""

    def __init__(
        self,
        ca_cert_path: str = "./certs/ca.pem",
        ca_key_path: str = "./certs/ca.key",
        device_certs_dir: str = "./data/device_certs",
        ca_key_size: int = 4096,
        leaf_key_size: int = 2048,
        cert_validity_days: int = 730,  # 2 years
    ):
        self.ca_cert_path = Path(ca_cert_path)
        self.ca_key_path = Path(ca_key_path)
        self.device_certs_dir = Path(device_certs_dir)
        self.ca_key_size = ca_key_size
        self.leaf_key_size = leaf_key_size
        self.cert_validity_days = cert_validity_days

        self._ca_cert: x509.Certificate | None = None
        self._ca_key: rsa.RSAPrivateKey | None = None
        self._cache: dict[str, tuple[str, str]] = {}  # hostname -> (cert_pem, key_pem)

        # Ensure directories exist
        self.ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
        self.device_certs_dir.mkdir(parents=True, exist_ok=True)

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
            with open(self.ca_key_path, "rb") as f:
                self._ca_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
            with open(self.ca_cert_path, "rb") as f:
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
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Ride the API Local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ride the API"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "XX"),
        ])

        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=self.cert_validity_days * 5))  # CA is long-lived
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    key_cert_sign=True, crl_sign=True,
                    digital_signature=False, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        self._ca_key = key
        self._ca_cert = cert

        self.ca_cert_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ca_key_path, "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        with open(self.ca_cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        logger.info("Generated new root CA at %s", self.ca_cert_path)

    def ca_cert_pem(self) -> str:
        """Return the CA certificate as a PEM string."""
        ca_cert, _ = self.ensure_ca()
        return ca_cert.public_bytes(serialization.Encoding.PEM).decode()

    # ── Leaf Certificate Management ────────────────────────────────────────────

    def get_cert_for_hostname(self, hostname: str) -> tuple[str, str]:
        """Get (cert_pem, key_pem) for a hostname, generating + caching if needed."""
        # Check in-memory cache
        if hostname in self._cache:
            return self._cache[hostname]

        # Check disk cache
        safe_name = self._safe_filename(hostname)
        cert_file = self.device_certs_dir / f"{safe_name}.pem"
        key_file = self.device_certs_dir / f"{safe_name}.key"

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

        now = datetime.now(timezone.utc)

        # Build subject/issuer
        try:
            ipaddress.ip_address(hostname)
            is_ip = True
        except ValueError:
            is_ip = False

        if is_ip:
            san = [x509.IPAddress(ipaddress.ip_address(hostname))]
            subject_name = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            ])
        else:
            san = [x509.DNSName(hostname)]
            # Try to add a wildcard for the domain
            if "." in hostname:
                parts = hostname.split(".")
                if len(parts) >= 2:
                    san.append(x509.DNSName(f"*.{'.'.join(parts[1:])}"))
            subject_name = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject_name)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=self.cert_validity_days))
            .add_extension(
                x509.SubjectAlternativeName(san), critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True,
                    content_commitment=False, data_encipherment=False,
                    key_cert_sign=False, crl_sign=False,
                    key_agreement=False, encipher_only=False, decipher_only=False,
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

        return {
            "ca_exists": self.ca_cert_path.exists(),
            "leaf_certs_cached": cert_count,
            "in_memory_cache": len(self._cache),
        }

    @staticmethod
    def _safe_filename(hostname: str) -> str:
        """Convert a hostname to a safe filename."""
        return hostname.replace("*", "wildcard_").replace(".", "_").replace(":", "_")


# ── Global instance ─────────────────────────────────────────────────────────

_cert_manager: CertManager | None = None


def get_cert_manager(
    ca_cert_path: str | None = None,
    ca_key_path: str | None = None,
    device_certs_dir: str | None = None,
) -> CertManager:
    """Get or create the global CertManager instance."""
    global _cert_manager
    if _cert_manager is None:
        _cert_manager = CertManager(
            ca_cert_path=ca_cert_path or "./certs/ca.pem",
            ca_key_path=ca_key_path or "./certs/ca.key",
            device_certs_dir=device_certs_dir or "./data/device_certs",
        )
    return _cert_manager