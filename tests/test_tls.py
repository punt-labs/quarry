"""Tests for quarry.tls — TLS certificate generation."""

from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID

from quarry.tls import (
    TLS_DIR,
    _write_file,
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

    def test_san_includes_ipv6_loopback(self) -> None:
        ca_cert, ca_key = self._make_ca()
        cert_pem, _ = generate_server_cert(ca_cert, ca_key, "myserver.local")
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ip_addrs = san.value.get_values_for_type(x509.IPAddress)
        assert ipaddress.IPv6Address("::1") in ip_addrs

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

    def test_partial_ca_state_only_crt_raises(self, tmp_path: Path) -> None:
        """ca.crt present but ca.key missing → ValueError, not silent regeneration."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir(parents=True)
        ca_cert_pem, _ = generate_ca("myhost.local")
        (tls_dir / "ca.crt").write_bytes(ca_cert_pem)
        # ca.key intentionally absent
        with (
            patch("quarry.tls.TLS_DIR", tls_dir),
            pytest.raises(ValueError, match="Partial CA state"),
        ):
            write_tls_files("myhost.local")

    def test_invalid_hostname_raises_friendly_error(self) -> None:
        """Hostname rejected by x509.DNSName raises ValueError with env-var hint."""
        ca_cert_pem, ca_key_pem = generate_ca("localhost")
        # Simulate a cryptography build that rejects the hostname as invalid.
        original_dns_name = x509.DNSName

        def _rejecting_dns_name(value: str) -> x509.DNSName:
            if value == "bad_host":
                raise ValueError("Invalid DNS label")
            return original_dns_name(value)

        with (
            patch("quarry.tls.x509.DNSName", side_effect=_rejecting_dns_name),
            pytest.raises(ValueError, match="QUARRY_TLS_HOSTNAME"),
        ):
            generate_server_cert(ca_cert_pem, ca_key_pem, "bad_host")

    def test_write_tls_files_returns_true_on_generation(self, tmp_path: Path) -> None:
        """First call generates files and returns True."""
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            assert write_tls_files("localhost") is True

    def test_write_tls_files_returns_false_when_all_exist(self, tmp_path: Path) -> None:
        """Second call finds all files present and returns False."""
        tls_dir = tmp_path / "tls"
        with patch("quarry.tls.TLS_DIR", tls_dir):
            write_tls_files("localhost")
            assert write_tls_files("localhost") is False

    def test_partial_ca_state_only_key_raises(self, tmp_path: Path) -> None:
        """ca.key present but ca.crt missing → ValueError, not silent regeneration."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir(parents=True)
        _, ca_key_pem = generate_ca("myhost.local")
        (tls_dir / "ca.key").write_bytes(ca_key_pem)
        # ca.crt intentionally absent
        with (
            patch("quarry.tls.TLS_DIR", tls_dir),
            pytest.raises(ValueError, match="Partial CA state"),
        ):
            write_tls_files("myhost.local")


class TestWriteFile:
    """Tests for _write_file — atomic write with fd-leak safety."""

    def test_writes_content_correctly(self, tmp_path: Path) -> None:
        target = tmp_path / "out.bin"
        data = b"hello world"
        _write_file(target, data, 0o600)
        assert target.read_bytes() == data

    def test_no_tmp_file_left_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "out.bin"
        _write_file(target, b"data", 0o600)
        # Fix 5: tmp name is "out.bin.tmp", not "out.tmp"
        assert not (tmp_path / "out.bin.tmp").exists()
        assert not (tmp_path / "out.tmp").exists()

    def test_tmp_name_uses_full_filename_not_suffix_replacement(
        self, tmp_path: Path
    ) -> None:
        """_write_file must use path.name + '.tmp', not path.with_suffix('.tmp').

        server.crt and server.key would both produce 'server.tmp' under the
        old with_suffix approach, causing a collision.  The correct names are
        'server.crt.tmp' and 'server.key.tmp'.
        """
        import os

        # Track which tmp paths were actually created.
        created_tmp: list[str] = []
        real_open = os.open

        def capturing_open(path: str, flags: int, mode: int = 0o777) -> int:
            created_tmp.append(path)
            return real_open(path, flags, mode)

        target = tmp_path / "server.crt"
        with patch("quarry.tls.os.open", side_effect=capturing_open):
            _write_file(target, b"data", 0o600)

        assert created_tmp, "os.open was not called"
        tmp_name = Path(created_tmp[0]).name
        assert tmp_name == "server.crt.tmp", (
            f"Expected 'server.crt.tmp' but got {tmp_name!r}. "
            "path.with_suffix('.tmp') would produce 'server.tmp', colliding "
            "with server.key's tmp file."
        )

    def test_fd_closed_when_fdopen_raises(self, tmp_path: Path) -> None:
        """If os.fdopen raises, the raw fd must be closed — not leaked."""
        target = tmp_path / "out.bin"
        closed_fds: list[int] = []
        raw_fd: list[int] = []

        real_os_open = os.open
        real_os_close = os.close

        def fake_os_open(path: str, flags: int, mode: int = 0o777) -> int:
            fd = real_os_open(path, flags, mode)
            raw_fd.append(fd)
            return fd

        def fake_os_fdopen(fd: int, *args: object, **kwargs: object) -> object:
            raise OSError("simulated resource exhaustion")

        def fake_os_close(fd: int) -> None:
            closed_fds.append(fd)
            real_os_close(fd)

        with (
            patch("quarry.tls.os.open", side_effect=fake_os_open),
            patch("quarry.tls.os.fdopen", side_effect=fake_os_fdopen),
            patch("quarry.tls.os.close", side_effect=fake_os_close),
            pytest.raises(OSError, match="simulated resource exhaustion"),
        ):
            _write_file(target, b"data", 0o600)

        assert raw_fd, "os.open was not called"
        assert raw_fd[0] in closed_fds, (
            f"fd {raw_fd[0]} was not closed after os.fdopen failure"
        )

    def test_tmp_file_removed_when_fdopen_raises(self, tmp_path: Path) -> None:
        """The .tmp sibling must be cleaned up when os.fdopen fails."""
        target = tmp_path / "out.bin"

        with (
            patch("quarry.tls.os.fdopen", side_effect=OSError("simulated")),
            pytest.raises(OSError),
        ):
            _write_file(target, b"data", 0o600)

        # Fix 5: tmp name is "out.bin.tmp", not "out.tmp"
        assert not (tmp_path / "out.bin.tmp").exists()
        assert not (tmp_path / "out.tmp").exists()

    def test_tmp_file_removed_when_write_raises(self, tmp_path: Path) -> None:
        """The .tmp sibling must be cleaned up when f.write() fails."""
        target = tmp_path / "out.bin"

        class _FailingWriter:
            """File-like that raises on write to simulate a write error."""

            def write(self, data: bytes) -> int:
                raise OSError("write failed")

            def __enter__(self) -> _FailingWriter:
                return self

            def __exit__(self, *_: object) -> None:
                pass

        def fdopen_failing(_fd: int, _mode: str = "r") -> _FailingWriter:
            # Close the real fd so the OS doesn't leak it in the test process.
            os.close(_fd)
            return _FailingWriter()

        with (
            patch("quarry.tls.os.fdopen", side_effect=fdopen_failing),
            pytest.raises(OSError, match="write failed"),
        ):
            _write_file(target, b"data", 0o600)

        assert not (tmp_path / "out.bin.tmp").exists()
