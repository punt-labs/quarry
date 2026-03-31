"""Tests for quarry.tls — TLS certificate generation."""

from __future__ import annotations

import ipaddress
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID

from quarry.tls import (
    TLS_DIR,
    cert_fingerprint,
    generate_ca,
    generate_server_cert,
    write_tls_files,
)


class TestGenerateCa:
    def test_returns_pem_bytes(self) -> None:
        cert_pem, key_pem = generate_ca("test.example.com")
        assert cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert key_pem.startswith(b"-----BEGIN EC PRIVATE KEY-----")

    def test_ca_constraint_set(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        cert = x509.load_pem_x509_certificate(cert_pem)
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True

    def test_hostname_in_cn(self) -> None:
        cert_pem, _ = generate_ca("myhost.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
        assert "myhost.local" in str(cn)

    def test_self_signed(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        cert = x509.load_pem_x509_certificate(cert_pem)
        assert cert.subject == cert.issuer

    def test_valid_for_10_years(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        cert = x509.load_pem_x509_certificate(cert_pem)
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        # Allow 1 day of slack for year boundary differences.
        assert delta.days >= 364 * 10

    def test_authority_key_identifier_present(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        cert = x509.load_pem_x509_certificate(cert_pem)
        aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        assert aki is not None

    def test_key_usage_key_cert_sign_and_crl_sign(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        cert = x509.load_pem_x509_certificate(cert_pem)
        ku_ext = cert.extensions.get_extension_for_class(x509.KeyUsage)
        assert ku_ext.critical is True
        ku = ku_ext.value
        assert ku.key_cert_sign is True
        assert ku.crl_sign is True
        assert ku.digital_signature is False
        assert ku.key_encipherment is False
        assert ku.key_agreement is False


class TestGenerateServerCert:
    def _make_ca(self) -> tuple[bytes, bytes]:
        return generate_ca("test.example.com")

    def test_returns_pem_bytes(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, key_pem = generate_server_cert(ca_cert, ca_key, "myserver.local")
        assert cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert key_pem.startswith(b"-----BEGIN EC PRIVATE KEY-----")

    def test_san_includes_localhost(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names

    def test_san_includes_hostname(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "myserver.local" in dns_names

    def test_san_includes_127_ip(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ip_addrs = san.value.get_values_for_type(x509.IPAddress)
        assert ipaddress.IPv4Address("127.0.0.1") in ip_addrs

    def test_ip_hostname_uses_ip_san_not_dns_san(self) -> None:
        """RFC 5280: IP address SANs must be iPAddress type, not dNSName."""
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "10.0.0.5")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ip_addrs = san.value.get_values_for_type(x509.IPAddress)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert ipaddress.IPv4Address("10.0.0.5") in ip_addrs
        assert "10.0.0.5" not in dns_names

    def test_ipv6_hostname_uses_ip_san_not_dns_san(self) -> None:
        """RFC 5280: IPv6 address SANs must be iPAddress type, not dNSName."""
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "2001:db8::1")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ip_addrs = san.value.get_values_for_type(x509.IPAddress)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert ipaddress.IPv6Address("2001:db8::1") in ip_addrs
        assert "2001:db8::1" not in dns_names

    def test_not_ca(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is False

    def test_signed_by_ca(self) -> None:
        ca_cert_pem, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert_pem, ca_key, "myserver.local")
        ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
        server_cert = x509.load_pem_x509_certificate(cert_pem)
        assert server_cert.issuer == ca_cert.subject

    def test_localhost_hostname_no_duplicate_san(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "localhost")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        # localhost should appear exactly once.
        assert dns_names.count("localhost") == 1

    def test_key_is_ec(self) -> None:
        ca_cert, ca_key = self._make_ca()
        from cryptography.hazmat.primitives import serialization

        _, key_pem = generate_server_cert(ca_cert, ca_key, "myserver.local")
        key = serialization.load_pem_private_key(key_pem, password=None)
        assert isinstance(key, EllipticCurvePrivateKey)

    def test_authority_key_identifier_present(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        assert aki is not None

    def test_key_usage_digital_signature_and_key_agreement(self) -> None:
        """EC keys must use key_agreement, not key_encipherment (RFC 5480)."""
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        ku_ext = cert.extensions.get_extension_for_class(x509.KeyUsage)
        assert ku_ext.critical is True
        ku = ku_ext.value
        assert ku.digital_signature is True
        assert ku.key_encipherment is False
        assert ku.key_cert_sign is False
        assert ku.crl_sign is False
        assert ku.key_agreement is True

    def test_extended_key_usage_server_auth(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        eku_ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert eku_ext.critical is False
        assert ExtendedKeyUsageOID.SERVER_AUTH in eku_ext.value


class TestCertFingerprint:
    def test_format(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        fp = cert_fingerprint(cert_pem)
        assert fp.startswith("SHA256:")
        hex_part = fp.removeprefix("SHA256:")
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_deterministic(self) -> None:
        cert_pem, _ = generate_ca("test.example.com")
        assert cert_fingerprint(cert_pem) == cert_fingerprint(cert_pem)

    def test_different_certs_differ(self) -> None:
        cert1, _ = generate_ca("host1.example.com")
        cert2, _ = generate_ca("host2.example.com")
        assert cert_fingerprint(cert1) != cert_fingerprint(cert2)


class TestWriteTlsFiles:
    def test_creates_all_four_files(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        assert (tls_dir / "ca.crt").exists()
        assert (tls_dir / "ca.key").exists()
        assert (tls_dir / "server.crt").exists()
        assert (tls_dir / "server.key").exists()

    def test_ca_crt_is_0644(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        mode = stat.S_IMODE((tls_dir / "ca.crt").stat().st_mode)
        assert mode == 0o644

    def test_private_files_are_0600(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        for name in ("ca.key", "server.crt", "server.key"):
            mode = stat.S_IMODE((tls_dir / name).stat().st_mode)
            assert mode == 0o600, f"{name} should be 0600, got {oct(mode)}"

    def test_idempotent(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
            mtime_ca = (tls_dir / "ca.crt").stat().st_mtime
            write_tls_files("myhost.local")
            # Files should not be regenerated — mtime unchanged.
            assert (tls_dir / "ca.crt").stat().st_mtime == mtime_ca

    def test_ca_cert_is_valid_pem(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        cert_pem = (tls_dir / "ca.crt").read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True

    def test_server_san_includes_localhost(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        cert_pem = (tls_dir / "server.crt").read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names

    def test_default_tls_dir_path(self) -> None:
        expected = Path.home() / ".punt-labs" / "quarry" / "tls"
        assert expected == TLS_DIR

    def test_reuses_existing_ca_when_server_certs_missing(self, tmp_path: Path) -> None:
        """Partial state: CA exists but server certs missing → reuses CA, no error."""
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        # Remove server certs to simulate partial state.
        (tls_dir / "server.crt").unlink()
        (tls_dir / "server.key").unlink()
        ca_crt_before = (tls_dir / "ca.crt").read_bytes()
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("myhost.local")
        # CA must not change — clients may have pinned it.
        assert (tls_dir / "ca.crt").read_bytes() == ca_crt_before
        assert (tls_dir / "server.crt").exists()
        assert (tls_dir / "server.key").exists()

    def test_ca_mismatch_raises_value_error(self, tmp_path: Path) -> None:
        """Mismatched ca.crt and ca.key files raise ValueError."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir(parents=True)
        # Generate two independent CA keypairs; use cert from one, key from another.
        ca_cert_pem_a, _ = generate_ca("host-a.local")
        _, ca_key_pem_b = generate_ca("host-b.local")
        (tls_dir / "ca.crt").write_bytes(ca_cert_pem_a)
        (tls_dir / "ca.key").write_bytes(ca_key_pem_b)
        with (
            patch("quarry.tls.TLS_DIR", tls_dir),
            pytest.raises(ValueError, match="do not match"),
        ):
            write_tls_files("myhost.local")
