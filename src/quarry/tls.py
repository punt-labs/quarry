"""TLS certificate generation for quarry remote connections.

Generates a self-signed CA and server certificate using EC P-256 keys.
Certificates are stored in ~/.punt-labs/quarry/tls/ and used by the
quarryd --tls command and the quarry login TOFU flow.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed448,
    ed25519,
    rsa,
)
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from quarry.home_paths import QuarryHome

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.types import (
        CertificatePublicKeyTypes,
    )

logger = logging.getLogger(__name__)


_CERT_VALID_YEARS = 10
_EC_CURVE = ec.SECP256R1()


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _signing_public_key(
    pub: CertificatePublicKeyTypes,
) -> (
    rsa.RSAPublicKey
    | ec.EllipticCurvePublicKey
    | dsa.DSAPublicKey
    | ed25519.Ed25519PublicKey
    | ed448.Ed448PublicKey
):
    """Narrow a certificate public key to the classic signing types.

    ``Certificate.public_key()`` can return any key type X.509 permits,
    including key-agreement keys (X25519, X448), post-quantum
    key-encapsulation keys (ML-KEM), and post-quantum signature keys
    (ML-DSA). Of these, only the five classic asymmetric types below can
    sign the certificates this CA issues. Key-agreement and ML-KEM keys
    cannot sign at all; ML-DSA keys are not produced by this CA. Anything
    outside the classic set is rejected here so a non-signing or
    unexpected key can never reach certificate signing. Our CA always
    uses EC P-256, so the guard is always satisfied in practice; it
    fails closed on any deviation.
    """
    if not isinstance(
        pub,
        (
            rsa.RSAPublicKey,
            ec.EllipticCurvePublicKey,
            dsa.DSAPublicKey,
            ed25519.Ed25519PublicKey,
            ed448.Ed448PublicKey,
        ),
    ):
        msg = f"CA public key must be a signing key, got {type(pub).__name__}"
        raise TypeError(msg)
    return pub


def _verify_ca_keypair(ca_cert_pem: bytes, ca_key_pem: bytes) -> None:
    """Reject a CA whose certificate and private key do not match.

    A mismatched ca.crt/ca.key pair yields server certificates no client
    can verify. Compare the public key embedded in the certificate against
    the public key derived from the private key; raise if they differ so
    the caller fails closed instead of writing unusable certs.
    """
    cert = x509.load_pem_x509_certificate(ca_cert_pem)
    key = serialization.load_pem_private_key(ca_key_pem, password=None)
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_pub != key_pub:
        msg = (
            f"CA cert and key at {QuarryHome.tls_dir()} do not match. "
            "Delete ca.crt and ca.key and re-run 'quarry install'."
        )
        raise ValueError(msg)


def generate_ca() -> tuple[bytes, bytes]:
    """Generate a self-signed CA keypair.

    Returns:
        (ca_cert_pem, ca_key_pem) as bytes.
    """
    key = ec.generate_private_key(_EC_CURVE)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Quarry CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Punt Labs"),
        ]
    )
    now = _now_utc()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_CERT_VALID_YEARS * 365))
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    ca_cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    ca_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return ca_cert_pem, ca_key_pem


def generate_server_cert(
    ca_cert_pem: bytes,
    ca_key_pem: bytes,
    hostname: str,
) -> tuple[bytes, bytes]:
    """Sign a server certificate with the given CA.

    The server cert always includes "localhost" as a SAN in addition to
    the provided hostname.

    Args:
        ca_cert_pem: The CA certificate in PEM format.
        ca_key_pem: The CA private key in PEM format.
        hostname: The primary hostname to include in the SAN.

    Returns:
        (server_cert_pem, server_key_pem) as bytes.
    """
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    if not isinstance(ca_key, EllipticCurvePrivateKey):
        msg = "CA key must be an EC private key"
        raise TypeError(msg)

    server_key = ec.generate_private_key(_EC_CURVE)

    # Build SAN list — always include localhost, add hostname if distinct.
    san_names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    if hostname not in ("localhost", "127.0.0.1", "::1"):
        try:
            san_names.append(x509.IPAddress(ipaddress.ip_address(hostname)))
        except ValueError:
            try:
                san_names.append(x509.DNSName(hostname))
            except ValueError as exc:
                msg = (
                    f"Hostname {hostname!r} is not a valid IP address or DNS label"
                    " for TLS certificates. Set the QUARRY_TLS_HOSTNAME environment"
                    " variable to a valid hostname or IP address and re-run"
                    " 'quarry install'."
                )
                raise ValueError(msg) from exc
    # Always include 127.0.0.1 and ::1 as IP SANs for loopback numeric access.
    san_names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    san_names.append(x509.IPAddress(ipaddress.ip_address("::1")))

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Punt Labs"),
        ]
    )
    now = _now_utc()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_CERT_VALID_YEARS * 365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                _signing_public_key(ca_cert.public_key())
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    server_key_pem = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return server_cert_pem, server_key_pem


def cert_fingerprint(cert_pem: bytes) -> str:
    """Return SHA256 fingerprint of a PEM certificate.

    Args:
        cert_pem: Certificate in PEM format.

    Returns:
        Fingerprint formatted as "SHA256:xxxx" where xxxx is the
        hex-encoded SHA256 digest of the DER-encoded certificate.
    """
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest()
    return f"SHA256:{digest}"


def write_tls_files(hostname: str) -> bool:
    """Generate CA + server cert, write to the TLS directory.

    Idempotent: skips generation if all four files already exist. If the CA
    already exists but server certs are missing (partial state), reuses the
    existing CA so clients that pinned it remain valid.

    File permissions:
        ca.crt  — 0644 (must be fetchable/served)
        ca.key  — 0600
        server.crt — 0600
        server.key — 0600

    Args:
        hostname: The server hostname (used in cert CN and SAN).

    Returns:
        True if new cert files were written, False if all files already existed.
    """
    tls_dir = QuarryHome.tls_dir()
    ca_crt_path = tls_dir / "ca.crt"
    ca_key_path = tls_dir / "ca.key"
    server_crt_path = tls_dir / "server.crt"
    server_key_path = tls_dir / "server.key"

    all_paths = (ca_crt_path, ca_key_path, server_crt_path, server_key_path)
    if all(p.exists() for p in all_paths):
        logger.debug("TLS files already exist at %s — skipping generation", tls_dir)
        return False

    # Private from the start, and made so if it already existed open: the keys
    # inside are 0600, but nobody else has any business listing the directory.
    tls_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tls_dir.chmod(0o700)

    # Reuse existing CA if present; only generate a new one if neither file exists.
    # Regenerating the CA would break all clients that pinned the old CA cert.
    ca_crt_exists = ca_crt_path.exists()
    ca_key_exists = ca_key_path.exists()

    if ca_crt_exists and ca_key_exists:
        logger.info("Reusing existing CA at %s", tls_dir)
        ca_cert_pem = ca_crt_path.read_bytes()
        ca_key_pem = ca_key_path.read_bytes()
        _verify_ca_keypair(ca_cert_pem, ca_key_pem)
    elif ca_crt_exists or ca_key_exists:
        # Partial CA state — refuse to proceed rather than silently overwrite.
        # Silently regenerating the CA would break clients that pinned the old cert.
        missing = ca_key_path if ca_crt_exists else ca_crt_path
        present = ca_crt_path if ca_crt_exists else ca_key_path
        msg = (
            f"Partial CA state at {tls_dir}: {present.name} exists but "
            f"{missing.name} is missing. Delete both files and re-run "
            "'quarry install' to regenerate."
        )
        raise ValueError(msg)
    else:
        logger.info("Generating new CA")
        ca_cert_pem, ca_key_pem = generate_ca()
        _write_file(ca_crt_path, ca_cert_pem, mode=0o644)
        _write_file(ca_key_path, ca_key_pem, mode=0o600)

    # Always regenerate server cert if we got here (some files were missing).
    logger.info("Generating server cert for hostname=%r", hostname)
    server_cert_pem, server_key_pem = generate_server_cert(
        ca_cert_pem, ca_key_pem, hostname
    )
    _write_file(server_crt_path, server_cert_pem, mode=0o600)
    _write_file(server_key_path, server_key_pem, mode=0o600)

    logger.info("TLS files written to %s", tls_dir)
    return True


def _write_file(path: Path, data: bytes, mode: int) -> None:
    """Write binary data to path atomically with the given permissions.

    Uses os.open() with mode so the file is created with the correct
    permissions from the start — no window where a private key is world-readable
    before chmod. Writes to a .tmp sibling then renames into place so
    idempotency checks on all-four-exist are never fooled by partial state.
    The .tmp file is removed on any exception.
    """
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, mode)
    try:
        f = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    try:
        with f:
            f.write(data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
