"""TLS certificate generation for quarry remote connections.

Generates a self-signed CA and server certificate using EC P-256 keys.
Certificates are stored in ~/.punt-labs/quarry/tls/ and used by the
quarry serve --tls command and the quarry login TOFU flow.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed448,
    ed25519,
    rsa,
    x448,
    x25519,
)
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

TLS_DIR: Path = Path.home() / ".punt-labs" / "quarry" / "tls"

_CERT_VALID_YEARS = 10
_EC_CURVE = ec.SECP256R1()


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _signing_public_key(
    pub: rsa.RSAPublicKey
    | ec.EllipticCurvePublicKey
    | dsa.DSAPublicKey
    | ed25519.Ed25519PublicKey
    | ed448.Ed448PublicKey
    | x25519.X25519PublicKey
    | x448.X448PublicKey,
) -> (
    rsa.RSAPublicKey
    | ec.EllipticCurvePublicKey
    | dsa.DSAPublicKey
    | ed25519.Ed25519PublicKey
    | ed448.Ed448PublicKey
):
    """Narrow a public key to the signing-key types accepted by AuthorityKeyIdentifier.

    X25519 and X448 are key-agreement keys and cannot sign certificates.
    Our CA always uses EC P-256, so this assertion is always satisfied.
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


def generate_ca(hostname: str) -> tuple[bytes, bytes]:
    """Generate a self-signed CA keypair.

    Args:
        hostname: The hostname that will be used in the CA subject CN.

    Returns:
        (ca_cert_pem, ca_key_pem) as bytes.
    """
    key = ec.generate_private_key(_EC_CURVE)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"Quarry CA ({hostname})"),
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
    """Generate CA + server cert, write to TLS_DIR.

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
    ca_crt_path = TLS_DIR / "ca.crt"
    ca_key_path = TLS_DIR / "ca.key"
    server_crt_path = TLS_DIR / "server.crt"
    server_key_path = TLS_DIR / "server.key"

    all_paths = (ca_crt_path, ca_key_path, server_crt_path, server_key_path)
    if all(p.exists() for p in all_paths):
        logger.debug("TLS files already exist at %s — skipping generation", TLS_DIR)
        return False

    TLS_DIR.mkdir(parents=True, exist_ok=True)

    # Reuse existing CA if present; only generate a new one if neither file exists.
    # Regenerating the CA would break all clients that pinned the old CA cert.
    ca_crt_exists = ca_crt_path.exists()
    ca_key_exists = ca_key_path.exists()

    if ca_crt_exists and ca_key_exists:
        logger.info("Reusing existing CA at %s", TLS_DIR)
        ca_cert_pem = ca_crt_path.read_bytes()
        ca_key_pem = ca_key_path.read_bytes()
        # Verify the CA keypair is consistent — mismatched files would produce
        # certificates that clients cannot verify.
        _ca_check = x509.load_pem_x509_certificate(ca_cert_pem)
        _ca_key_check = serialization.load_pem_private_key(ca_key_pem, password=None)
        _cert_pub = _ca_check.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        _key_pub = _ca_key_check.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if _cert_pub != _key_pub:
            msg = (
                f"CA cert and key at {TLS_DIR} do not match. "
                "Delete ca.crt and ca.key and re-run 'quarry install'."
            )
            raise ValueError(msg)
    elif ca_crt_exists or ca_key_exists:
        # Partial CA state — refuse to proceed rather than silently overwrite.
        # Silently regenerating the CA would break clients that pinned the old cert.
        missing = ca_key_path if ca_crt_exists else ca_crt_path
        present = ca_crt_path if ca_crt_exists else ca_key_path
        msg = (
            f"Partial CA state at {TLS_DIR}: {present.name} exists but "
            f"{missing.name} is missing. Delete both files and re-run "
            "'quarry install' to regenerate."
        )
        raise ValueError(msg)
    else:
        logger.info("Generating new CA for hostname=%r", hostname)
        ca_cert_pem, ca_key_pem = generate_ca(hostname)
        _write_file(ca_crt_path, ca_cert_pem, mode=0o644)
        _write_file(ca_key_path, ca_key_pem, mode=0o600)

    # Always regenerate server cert if we got here (some files were missing).
    logger.info("Generating server cert for hostname=%r", hostname)
    server_cert_pem, server_key_pem = generate_server_cert(
        ca_cert_pem, ca_key_pem, hostname
    )
    _write_file(server_crt_path, server_cert_pem, mode=0o600)
    _write_file(server_key_path, server_key_pem, mode=0o600)

    logger.info("TLS files written to %s", TLS_DIR)
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
