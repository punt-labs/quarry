"""Behaviour of :class:`ReadPayload` and :class:`ReadCaptureFilter`.

Read fires often and has the highest secret-leak surface — filter tests
exercise each of the four admission checks independently and together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from quarry.read_capture import ReadCaptureFilter, ReadPayload

if TYPE_CHECKING:
    from quarry.collection_resolver import CollectionResolver
    from quarry.sync_registry import DirectoryRegistration


class TestReadPayloadFilePath:
    def test_extracts_file_path(self) -> None:
        payload = ReadPayload({"tool_input": {"file_path": "/tmp/x.md"}})
        assert payload.file_path == "/tmp/x.md"

    def test_returns_none_for_missing_tool_input(self) -> None:
        assert ReadPayload({}).file_path is None

    def test_returns_none_for_non_dict_tool_input(self) -> None:
        assert ReadPayload({"tool_input": "not a dict"}).file_path is None

    def test_returns_none_for_blank_path(self) -> None:
        assert ReadPayload({"tool_input": {"file_path": "  "}}).file_path is None

    def test_returns_none_for_non_string_path(self) -> None:
        assert ReadPayload({"tool_input": {"file_path": 123}}).file_path is None


class TestReadPayloadContent:
    def test_extracts_bare_string_response(self) -> None:
        payload = ReadPayload(
            {"tool_input": {"file_path": "/tmp/x.md"}, "tool_response": "hello"}
        )
        assert payload.content == "hello"

    def test_extracts_from_json_content_field(self) -> None:
        payload = ReadPayload(
            {
                "tool_input": {"file_path": "/tmp/x.md"},
                "tool_response": json.dumps({"content": "the body"}),
            }
        )
        assert payload.content == "the body"

    def test_extracts_from_json_text_field(self) -> None:
        payload = ReadPayload(
            {
                "tool_input": {"file_path": "/tmp/x.md"},
                "tool_response": json.dumps({"text": "the body"}),
            }
        )
        assert payload.content == "the body"

    def test_returns_none_for_missing_response(self) -> None:
        assert ReadPayload({"tool_input": {"file_path": "/tmp/x.md"}}).content is None

    def test_returns_none_for_non_string_response(self) -> None:
        payload = ReadPayload(
            {"tool_input": {"file_path": "/tmp/x.md"}, "tool_response": 42}
        )
        assert payload.content is None


class TestFilterSecretPathDenylist:
    """Denylist runs against filename fragments, case-insensitive."""

    def _filter(self) -> ReadCaptureFilter:
        return ReadCaptureFilter(resolver=None)

    def test_rejects_dotenv_exact(self) -> None:
        assert self._filter().should_capture("/home/user/.env", cwd="") is False

    def test_rejects_dotenv_variant(self) -> None:
        assert self._filter().should_capture("/home/user/.env.local", cwd="") is False

    def test_rejects_id_rsa(self) -> None:
        assert self._filter().should_capture("/home/user/.ssh/id_rsa", cwd="") is False

    def test_rejects_id_ed25519(self) -> None:
        assert (
            self._filter().should_capture("/root/.ssh/id_ed25519.pub", cwd="") is False
        )

    def test_rejects_pem_files(self) -> None:
        assert self._filter().should_capture("/etc/ssl/private.pem", cwd="") is False

    def test_rejects_key_files(self) -> None:
        assert self._filter().should_capture("/etc/ssl/private.key", cwd="") is False

    def test_rejects_known_hosts(self) -> None:
        assert (
            self._filter().should_capture("/home/user/.ssh/known_hosts", cwd="")
            is False
        )

    def test_rejects_netrc(self) -> None:
        assert self._filter().should_capture("/home/user/.netrc", cwd="") is False

    def test_rejects_aws_credentials(self) -> None:
        assert (
            self._filter().should_capture("/home/user/.aws/credentials", cwd="")
            is False
        )

    def test_rejects_any_path_under_ssh_dir(self) -> None:
        assert (
            self._filter().should_capture("/home/user/.ssh/config.md", cwd="") is False
        )

    def test_secret_check_is_case_insensitive(self) -> None:
        assert self._filter().should_capture("/root/.SSH/ID_RSA", cwd="") is False

    def test_rejects_credentials_md(self) -> None:
        assert self._filter().should_capture("/tmp/credentials.md", cwd="") is False

    def test_rejects_passwords_txt(self) -> None:
        assert self._filter().should_capture("/tmp/passwords.txt", cwd="") is False

    def test_rejects_secrets_md(self) -> None:
        assert self._filter().should_capture("/tmp/secrets.md", cwd="") is False

    def test_rejects_api_keys_md(self) -> None:
        assert self._filter().should_capture("/tmp/api-keys.md", cwd="") is False

    def test_rejects_id_ecdsa(self) -> None:
        assert self._filter().should_capture("/home/u/.ssh/id_ecdsa", cwd="") is False

    def test_rejects_pfx_files(self) -> None:
        assert self._filter().should_capture("/tmp/client.pfx", cwd="") is False

    def test_rejects_p12_files(self) -> None:
        assert self._filter().should_capture("/tmp/client.p12", cwd="") is False

    def test_rejects_gnupg_dir(self) -> None:
        assert self._filter().should_capture("/home/u/.gnupg/notes.md", cwd="") is False

    def test_rejects_kube_dir(self) -> None:
        assert self._filter().should_capture("/home/u/.kube/config.md", cwd="") is False

    def test_rejects_docker_dir(self) -> None:
        assert (
            self._filter().should_capture("/home/u/.docker/notes.md", cwd="") is False
        )


class TestFilterSymlinkResolution:
    """The denylist runs on the RESOLVED path — a symlink cannot hide a secret."""

    def test_md_symlink_to_dotenv_is_rejected(self, tmp_path: Path) -> None:
        secret = tmp_path / ".env"
        secret.write_text("SECRET=1")
        link = tmp_path / "notes.md"
        link.symlink_to(secret)
        f = ReadCaptureFilter(resolver=None)
        assert f.should_capture(str(link), cwd="") is False

    def test_md_symlink_to_ordinary_file_is_accepted(self, tmp_path: Path) -> None:
        target = tmp_path / "report.md"
        target.write_text("nothing secret here")
        link = tmp_path / "alias.md"
        link.symlink_to(target)
        f = ReadCaptureFilter(resolver=None)
        assert f.should_capture(str(link), cwd="") is True


class TestFilterExtensionAllowlist:
    """Only prose formats the loaders understand are captured."""

    def _filter(self) -> ReadCaptureFilter:
        return ReadCaptureFilter(resolver=None)

    def test_rejects_python_source(self) -> None:
        assert self._filter().should_capture("/tmp/module.py", cwd="") is False

    def test_rejects_json(self) -> None:
        assert self._filter().should_capture("/tmp/data.json", cwd="") is False

    def test_rejects_log(self) -> None:
        assert self._filter().should_capture("/var/log/x.log", cwd="") is False

    def test_accepts_markdown(self) -> None:
        assert self._filter().should_capture("/tmp/doc.md", cwd="") is True

    def test_accepts_text(self) -> None:
        assert self._filter().should_capture("/tmp/notes.txt", cwd="") is True

    def test_accepts_rst(self) -> None:
        assert self._filter().should_capture("/tmp/index.rst", cwd="") is True

    def test_accepts_pdf(self) -> None:
        assert self._filter().should_capture("/tmp/spec.pdf", cwd="") is True

    def test_accepts_docx(self) -> None:
        assert self._filter().should_capture("/tmp/contract.docx", cwd="") is True


class TestFilterSizeCap:
    """Byte cap rejects large content to protect the ingest queue."""

    def _filter(self) -> ReadCaptureFilter:
        return ReadCaptureFilter(resolver=None)

    def test_accepts_under_cap(self) -> None:
        assert (
            self._filter().should_capture("/tmp/doc.md", cwd="", content_bytes=100)
            is True
        )

    def test_rejects_over_cap(self) -> None:
        assert (
            self._filter().should_capture("/tmp/doc.md", cwd="", content_bytes=250_000)
            is False
        )

    def test_none_bytes_defers_check(self) -> None:
        # None means the caller has not yet loaded the file; other checks
        # still apply, but size cannot reject yet.
        assert (
            self._filter().should_capture("/tmp/doc.md", cwd="", content_bytes=None)
            is True
        )


class TestFilterInTreeExclusion:
    """Paths under a registered collection are already indexed by session-start sync."""

    def _resolver_for(self, registered_dir: str) -> CollectionResolver:
        resolver = MagicMock()
        registration = _fake_registration(registered_dir)
        resolver.covering_registration.return_value = registration
        return cast("CollectionResolver", resolver)

    def test_rejects_path_inside_registered_tree(self, tmp_path: Path) -> None:
        registered = tmp_path / "project"
        registered.mkdir()
        target = registered / "docs" / "spec.md"
        target.parent.mkdir(parents=True)
        target.touch()
        f = ReadCaptureFilter(resolver=self._resolver_for(str(registered)))
        assert f.should_capture(str(target), cwd=str(registered)) is False

    def test_accepts_path_outside_registered_tree(self, tmp_path: Path) -> None:
        registered = tmp_path / "project"
        registered.mkdir()
        target = tmp_path / "elsewhere" / "notes.md"
        target.parent.mkdir(parents=True)
        target.touch()
        f = ReadCaptureFilter(resolver=self._resolver_for(str(registered)))
        assert f.should_capture(str(target), cwd=str(registered)) is True

    def test_accepts_when_no_covering_registration(self, tmp_path: Path) -> None:
        resolver = MagicMock()
        resolver.covering_registration.return_value = None
        target = tmp_path / "doc.md"
        target.touch()
        f = ReadCaptureFilter(resolver=resolver)
        assert f.should_capture(str(target), cwd=str(tmp_path)) is True

    def test_no_resolver_treats_as_out_of_tree(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.md"
        target.touch()
        assert (
            ReadCaptureFilter(resolver=None).should_capture(
                str(target), cwd=str(tmp_path)
            )
            is True
        )


class TestFilterHappyPath:
    def test_out_of_tree_pdf_under_cap_and_not_secret(self, tmp_path: Path) -> None:
        target = tmp_path / "vendor-api-spec.pdf"
        target.touch()
        f = ReadCaptureFilter(resolver=None)
        assert f.should_capture(str(target), cwd="", content_bytes=1024) is True


def _fake_registration(directory: str) -> DirectoryRegistration:
    """Return a MagicMock that quacks like a DirectoryRegistration."""
    reg = MagicMock()
    reg.directory = directory
    return reg
