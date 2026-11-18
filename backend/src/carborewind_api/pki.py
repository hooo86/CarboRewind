"""Internal certificate authority for agent mTLS.

* The CA (ECDSA P-256) is created once (``carborewind-api pki-init``) in ``pki_dir``; its
  private key is a 0600 file readable only by the API user.
* An agent generates its own key pair and sends a CSR at enrolment; the private key never
  leaves the Carbonio host. The API signs a short-lived client certificate (CN = agent id,
  EKU clientAuth) and stores its SHA-256 fingerprint.
* TLS (and the client-certificate check against this CA) is terminated by the reverse
  proxy, which forwards the verified certificate; the API then matches its fingerprint with
  an active agent (see ``api/agent.py``). Revocation = clearing the fingerprint.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERT = "ca.crt"
CA_KEY = "ca.key"


class PkiError(Exception):
    pass


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def fingerprint_pem(pem: str | bytes) -> tuple[str, datetime]:
    """Fingerprint and expiry of a PEM certificate (raises PkiError if unparseable)."""
    data = pem.encode() if isinstance(pem, str) else pem
    try:
        cert = x509.load_pem_x509_certificate(data)
    except ValueError as exc:
        raise PkiError("invalid certificate") from exc
    return fingerprint(cert), cert.not_valid_after_utc


def init_ca(directory: Path, common_name: str = "CarboRewind agents CA", days: int = 3650) -> None:
    """Create the CA in an empty directory (refuses to overwrite)."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (directory / CA_KEY).exists():
        raise PkiError(f"a CA already exists in {directory}")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(directory / CA_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key_pem)
    (directory / CA_CERT).write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@dataclass(frozen=True)
class IssuedCert:
    pem: str
    fingerprint: str
    not_after: datetime


class CertificateAuthority:
    def __init__(self, directory: Path) -> None:
        try:
            self._cert = x509.load_pem_x509_certificate((directory / CA_CERT).read_bytes())
            key = serialization.load_pem_private_key((directory / CA_KEY).read_bytes(), None)
        except FileNotFoundError as exc:
            raise PkiError(f"no CA in {directory}: run `carborewind-api pki-init`") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise PkiError("unsupported CA key type")
        self._key = key

    @property
    def ca_pem(self) -> str:
        return self._cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign_agent_csr(self, csr_pem: str, agent_id: str, days: int) -> IssuedCert:
        """Sign a client certificate for ``agent_id``. The CSR's own subject is ignored."""
        try:
            csr = x509.load_pem_x509_csr(csr_pem.encode())
        except ValueError as exc:
            raise PkiError("invalid CSR") from exc
        if not csr.is_signature_valid:
            raise PkiError("CSR signature is invalid")
        public_key = csr.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or public_key.key_size < 256:
            raise PkiError("agent key must be ECDSA P-256 or stronger")
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CarboRewind"),
                        x509.NameAttribute(NameOID.COMMON_NAME, f"agent:{agent_id}"),
                    ]
                )
            )
            .issuer_name(self._cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .sign(self._key, hashes.SHA256())
        )
        return IssuedCert(
            pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
            fingerprint=fingerprint(cert),
            not_after=cert.not_valid_after_utc,
        )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
