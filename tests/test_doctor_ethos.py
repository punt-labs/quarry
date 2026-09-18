"""Behaviour of the ethos ext guide writer and its install-step wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from quarry.doctor_ethos import EthosExtDiagnostics
from quarry.ethos_ext_block import MEMORY_GUIDE_HEADER
from quarry.ethos_ext_scan import ExtScanOutcome, ExtWriteResult

_V1_BLOCK = (
    "\nsession_context: |\n  ## Memory\n  \n  You have persistent memory stored in "
    "quarry, a local semantic\n  search engine.\n"
)


def _make_ext(identities_dir: Path, handle: str) -> Path:
    ext_dir = identities_dir / f"{handle}.ext"
    ext_dir.mkdir(parents=True, exist_ok=True)
    return ext_dir


def _write_ext(identities_dir: Path, handle: str, text: str) -> Path:
    quarry_yaml = _make_ext(identities_dir, handle) / "quarry.yaml"
    quarry_yaml.write_text(text, encoding="utf-8")
    return quarry_yaml


class TestConfigure:
    """The ``quarry install`` step: global tree only, best-effort, one line."""

    def test_writes_session_context_when_missing(self, tmp_path: Path) -> None:
        identities_dir = tmp_path / "identities"
        quarry_yaml = _write_ext(
            identities_dir,
            "claude",
            yaml.dump({"memory_collection": "claude-memories"}),
        )

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        assert result.passed is True
        data = yaml.safe_load(quarry_yaml.read_text(encoding="utf-8"))
        assert "session_context" in data
        assert "claude-memories" in data["session_context"]
        assert "claude" in data["session_context"]
        assert "updated" in result.message
        assert "claude" in result.message

    def test_idempotent_when_session_context_exists(self, tmp_path: Path) -> None:
        identities_dir = tmp_path / "identities"
        original = {
            "memory_collection": "jfreeman-memories",
            "session_context": "existing content, do not overwrite",
        }
        quarry_yaml = _write_ext(identities_dir, "jfreeman", yaml.dump(original))

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        data = yaml.safe_load(quarry_yaml.read_text(encoding="utf-8"))
        assert data["session_context"] == "existing content, do not overwrite"
        assert "already" in result.message

    def test_skips_identity_without_quarry_yaml(self, tmp_path: Path) -> None:
        identities_dir = tmp_path / "identities"
        _make_ext(identities_dir, "nomemory")

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        assert result.passed is True
        assert "no identities" in result.message

    def test_ethos_not_installed(self, tmp_path: Path) -> None:
        result = EthosExtDiagnostics.configure(identities_dir=tmp_path / "nonexistent")

        assert result.passed is True
        assert result.required is False
        assert "ethos not installed" in result.message

    def test_identities_dir_is_a_file_skips_gracefully(self, tmp_path: Path) -> None:
        """A path that exists but is not a directory must soft-skip, not raise."""
        not_a_dir = tmp_path / "identities"
        not_a_dir.write_text("not a directory", encoding="utf-8")

        result = EthosExtDiagnostics.configure(identities_dir=not_a_dir)

        assert result.passed is True
        assert result.required is False
        assert "ethos not installed" in result.message

    def test_defaults_to_the_global_tree(self, tmp_path: Path) -> None:
        """No argument means the operator's global identities, never a repo's."""
        with patch(
            "quarry.doctor_ethos.EthosTree.global_identities",
            return_value=tmp_path / "absent",
        ):
            result = EthosExtDiagnostics.configure()
        assert "ethos not installed" in result.message

    def test_two_identities_one_needs_update(self, tmp_path: Path) -> None:
        identities_dir = tmp_path / "identities"
        claude_yaml = _write_ext(
            identities_dir, "claude", yaml.dump({"memory_collection": "claude-col"})
        )
        jf_yaml = _write_ext(
            identities_dir,
            "jfreeman",
            yaml.dump(
                {"memory_collection": "jf-col", "session_context": "already here"}
            ),
        )

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        assert result.passed is True
        assert "updated 1 identity: claude" in result.message
        assert "already set: jfreeman" in result.message
        assert "session_context" in yaml.safe_load(claude_yaml.read_text())
        assert yaml.safe_load(jf_yaml.read_text())["session_context"] == "already here"

    def test_no_collection_surfaced_in_message(self, tmp_path: Path) -> None:
        """quarry.yaml with no memory_collection surfaces in result, not silent."""
        identities_dir = tmp_path / "identities"
        _write_ext(identities_dir, "ghost", "other_key: value\n")

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        assert result.passed is True
        assert "no memory_collection" in result.message
        assert "ghost" in result.message

    def test_per_identity_failure_isolates_others(self, tmp_path: Path) -> None:
        """A bad quarry.yaml for one identity does not skip processing the next."""
        identities_dir = tmp_path / "identities"
        bad_dir = _make_ext(identities_dir, "aardvark")
        (bad_dir / "quarry.yaml").write_bytes(b"key: [\x00invalid")
        good_yaml = _write_ext(
            identities_dir, "zebra", yaml.dump({"memory_collection": "z-col"})
        )

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        assert "zebra" in result.message
        assert "errors" in result.message
        assert "aardvark" in result.message
        assert result.passed is False
        assert "session_context" in yaml.safe_load(good_yaml.read_text())

    def test_raw_append_preserves_existing_comments(self, tmp_path: Path) -> None:
        """Appending session_context must not destroy existing YAML comments."""
        identities_dir = tmp_path / "identities"
        original_text = "# important comment\nmemory_collection: tester-col\n"
        quarry_yaml = _write_ext(identities_dir, "tester", original_text)

        EthosExtDiagnostics.configure(identities_dir=identities_dir)

        updated_text = quarry_yaml.read_text(encoding="utf-8")
        assert "# important comment" in updated_text
        data = yaml.safe_load(updated_text)
        assert "session_context" in data
        assert "tester-col" in data["session_context"]

    def test_non_mapping_yaml_surfaced_as_no_collection(self, tmp_path: Path) -> None:
        """Non-mapping quarry.yaml is treated as no_collection, not failed.

        Isolation is confirmed: the adjacent valid identity still gets updated.
        """
        identities_dir = tmp_path / "identities"
        _write_ext(identities_dir, "listident", "- item1\n- item2\n")
        good_yaml = _write_ext(
            identities_dir, "zvalid", yaml.dump({"memory_collection": "zvalid-col"})
        )

        result = EthosExtDiagnostics.configure(identities_dir=identities_dir)

        assert "no memory_collection" in result.message
        assert "listident" in result.message
        assert "errors" not in result.message
        assert "zvalid" in result.message
        assert "session_context" in yaml.safe_load(good_yaml.read_text())


class TestRefresh:
    """The scanner both install and enable share."""

    def test_v1_block_refreshes_to_v2_once(self, tmp_path: Path) -> None:
        """A stale guide is spliced up exactly once; the second pass is a no-op."""
        identities_dir = tmp_path / "identities"
        quarry_yaml = _write_ext(
            identities_dir, "rmh", "memory_collection: memory-rmh\n" + _V1_BLOCK
        )

        first = EthosExtDiagnostics.refresh(identities_dir)
        after_first = quarry_yaml.read_text()
        second = EthosExtDiagnostics.refresh(identities_dir)

        assert first.updated == ("rmh",)
        assert second.already_set == ("rmh",)
        assert quarry_yaml.read_text() == after_first
        body = yaml.safe_load(after_first)["session_context"]
        assert body.startswith(MEMORY_GUIDE_HEADER)
        assert "### Working Memory" not in body

    def test_custom_block_is_never_overwritten(self, tmp_path: Path) -> None:
        identities_dir = tmp_path / "identities"
        raw = "memory_collection: m\nsession_context: |\n  You are the ops lead.\n"
        quarry_yaml = _write_ext(identities_dir, "ops", raw)

        outcome = EthosExtDiagnostics.refresh(identities_dir)

        assert outcome.already_set == ("ops",)
        assert quarry_yaml.read_text() == raw

    def test_absent_directory_is_an_empty_outcome(self, tmp_path: Path) -> None:
        outcome = EthosExtDiagnostics.refresh(tmp_path / "nowhere")
        assert outcome == ExtScanOutcome()
        assert outcome.is_empty

    def test_never_creates_an_ext_file(self, tmp_path: Path) -> None:
        """An identity dir without quarry.yaml is skipped — refresh is not create."""
        identities_dir = tmp_path / "identities"
        _make_ext(identities_dir, "nomemory")
        (identities_dir / "nomemory.yaml").write_text("name: nomemory\n")

        outcome = EthosExtDiagnostics.refresh(identities_dir)

        assert outcome.is_empty
        assert not (identities_dir / "nomemory.ext" / "quarry.yaml").exists()

    def test_non_utf8_file_is_a_failure_entry(self, tmp_path: Path) -> None:
        identities_dir = tmp_path / "identities"
        bad = _make_ext(identities_dir, "alice") / "quarry.yaml"
        bad.write_bytes(b"memory_collection: \xff\xfe bad\n")

        outcome = EthosExtDiagnostics.refresh(identities_dir)

        assert outcome.failed_handles == ("alice",)
        assert "codec" in outcome.failed[0].reason

    def test_non_io_bug_propagates(self, tmp_path: Path) -> None:
        """The catch is narrowed: a programming error is not a per-identity failure."""
        identities_dir = tmp_path / "identities"
        _write_ext(identities_dir, "rmh", "memory_collection: memory-rmh\n")
        with (
            patch(
                "quarry.doctor_ethos.EthosExtDiagnostics.write_session_context",
                side_effect=TypeError("bug"),
            ),
            pytest.raises(TypeError, match="bug"),
        ):
            EthosExtDiagnostics.refresh(identities_dir)


class TestWriteSessionContext:
    """The single-file writer goes through AtomicFile (bug class 1)."""

    def test_returns_the_typed_result(self, tmp_path: Path) -> None:
        quarry_yaml = tmp_path / "quarry.yaml"
        quarry_yaml.write_text("memory_collection: memory-rmh\n")
        assert (
            EthosExtDiagnostics.write_session_context(quarry_yaml, "rmh")
            is ExtWriteResult.UPDATED
        )
        assert (
            EthosExtDiagnostics.write_session_context(quarry_yaml, "rmh")
            is ExtWriteResult.ALREADY_SET
        )

    def test_writes_through_atomic_replace(self, tmp_path: Path) -> None:
        quarry_yaml = tmp_path / "quarry.yaml"
        quarry_yaml.write_text("memory_collection: memory-rmh\n")
        with patch("quarry.doctor_ethos.AtomicFile.replace") as replace:
            EthosExtDiagnostics.write_session_context(quarry_yaml, "rmh")
        replace.assert_called_once()
        assert MEMORY_GUIDE_HEADER in replace.call_args.args[0]

    def test_failed_replace_leaves_the_stale_block_intact(self, tmp_path: Path) -> None:
        quarry_yaml = tmp_path / "quarry.yaml"
        original = "memory_collection: memory-rmh\n" + _V1_BLOCK
        quarry_yaml.write_text(original)
        with (
            patch(
                "quarry.doctor_ethos.AtomicFile.replace", side_effect=OSError("disk")
            ),
            pytest.raises(OSError, match="disk"),
        ):
            EthosExtDiagnostics.write_session_context(quarry_yaml, "rmh")
        assert quarry_yaml.read_text() == original
        assert not list(tmp_path.glob(".quarry.yaml.*.tmp"))

    def test_mode_is_preserved(self, tmp_path: Path) -> None:
        quarry_yaml = tmp_path / "quarry.yaml"
        quarry_yaml.write_text("memory_collection: memory-rmh\n")
        quarry_yaml.chmod(0o600)
        EthosExtDiagnostics.write_session_context(quarry_yaml, "rmh")
        assert quarry_yaml.stat().st_mode & 0o777 == 0o600
