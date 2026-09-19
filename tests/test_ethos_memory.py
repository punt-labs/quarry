"""Tests for the ethos-memory bootstrap."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import yaml

from quarry.ethos_ext_block import MEMORY_GUIDE_HEADER
from quarry.ethos_memory import EthosMemoryBootstrap, EthosMemoryResult

if TYPE_CHECKING:
    import pytest

_V1_BLOCK = "\nsession_context: |\n  ## Memory\n  \n  old guide body\n"


def _identities(root: Path, *handles: str) -> Path:
    identities = root / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    for handle in handles:
        (identities / f"{handle}.yaml").write_text(f"agent: {handle}\n")
    return identities


def _vendored(root: Path, *handles: str, block: str = _V1_BLOCK) -> Path:
    """Build a repo root with a vendored tree whose exts carry *block*."""
    identities = root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True)
    for handle in handles:
        (identities / f"{handle}.yaml").write_text(f"agent: {handle}\n")
        ext = identities / f"{handle}.ext"
        ext.mkdir()
        (ext / "quarry.yaml").write_text(f"memory_collection: memory-{handle}\n{block}")
    return identities


def test_result_memory_collections_derived_from_created() -> None:
    result = EthosMemoryResult(created=["claude", "rmh"])
    assert result.memory_collections == ["memory-claude", "memory-rmh"]


def test_skips_when_identities_dir_missing(tmp_path: Path) -> None:
    result = EthosMemoryBootstrap(tmp_path / "nonexistent").run()
    assert result.skipped is True
    assert result.created == []


def test_creates_quarry_yaml_files(tmp_path: Path) -> None:
    identities = _identities(tmp_path, "claude", "rmh")

    result = EthosMemoryBootstrap(identities).run()

    assert result.skipped is False
    assert result.failed == []
    assert "claude" in result.created
    assert "rmh" in result.created
    assert set(result.updated) == {"claude", "rmh"}
    assert result.already_set == []
    assert result.vendored_updated == []

    claude_yaml = identities / "claude.ext" / "quarry.yaml"
    rmh_yaml = identities / "rmh.ext" / "quarry.yaml"
    assert "memory_collection: memory-claude" in claude_yaml.read_text()
    assert "memory_collection: memory-rmh" in rmh_yaml.read_text()


def test_existing_quarry_yaml_not_modified(tmp_path: Path) -> None:
    identities = _identities(tmp_path, "claude")
    ext_dir = identities / "claude.ext"
    ext_dir.mkdir()
    quarry_yaml = ext_dir / "quarry.yaml"
    quarry_yaml.write_text("memory_collection: wrong-name\n")

    result = EthosMemoryBootstrap(identities).run()

    assert result.skipped is False
    assert "claude" not in result.created
    assert "memory_collection: wrong-name" in quarry_yaml.read_text()


def test_stale_global_guide_is_refreshed(tmp_path: Path) -> None:
    identities = _identities(tmp_path, "rmh")
    ext_dir = identities / "rmh.ext"
    ext_dir.mkdir()
    (ext_dir / "quarry.yaml").write_text("memory_collection: memory-rmh\n" + _V1_BLOCK)

    result = EthosMemoryBootstrap(identities).run()

    assert result.updated == ["rmh"]
    body = yaml.safe_load((ext_dir / "quarry.yaml").read_text())["session_context"]
    assert body.startswith(MEMORY_GUIDE_HEADER)


def test_bad_yaml_is_recorded_and_bootstrap_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identities = _identities(tmp_path, "alice", "bad")

    from yaml import YAMLError

    from quarry.doctor_ethos import EthosExtDiagnostics
    from quarry.ethos_ext_scan import ExtWriteResult

    original_write = EthosExtDiagnostics.write_session_context

    def selective_raise(quarry_yaml: Path, handle: str) -> ExtWriteResult:
        if handle == "bad":
            msg = "simulated YAML parse failure"
            raise YAMLError(msg)
        return original_write(quarry_yaml, handle)

    monkeypatch.setattr(
        "quarry.doctor_ethos.EthosExtDiagnostics.write_session_context",
        selective_raise,
    )

    result = EthosMemoryBootstrap(identities).run()

    assert result.skipped is False
    assert "alice" in result.created
    # bad's quarry.yaml file was written (so it's "created"), but the
    # session_context write raised — it lands in failed, never updated.
    assert "bad" in result.created
    assert "bad" in result.failed
    assert "bad" not in result.updated
    assert "bad" not in result.already_set
    assert (identities / "alice.ext" / "quarry.yaml").exists()
    assert (identities / "bad.ext" / "quarry.yaml").exists()


def test_non_utf8_identity_file_recorded_not_fatal(tmp_path: Path) -> None:
    # A non-UTF8/corrupt ext quarry.yaml makes the reader raise UnicodeDecodeError
    # (a ValueError, not OSError). The bootstrap records the handle and continues.
    identities = _identities(tmp_path, "alice")
    ext_dir = identities / "alice.ext"
    ext_dir.mkdir()
    (ext_dir / "quarry.yaml").write_bytes(b"memory_collection: \xff\xfe bad\n")

    result = EthosMemoryBootstrap(identities).run()

    assert result.skipped is False
    assert "alice" in result.failed
    assert "alice" not in result.updated
    assert "alice" not in result.already_set


class TestForRepo:
    """``quarry enable`` refreshes the repo's vendored tree; it never creates there."""

    def test_no_vendored_tree_is_global_only(self, unpinned_root: Path) -> None:
        identities = _identities(unpinned_root / "global", "rmh")
        with patch("quarry.ethos_memory._GLOBAL_IDENTITIES", identities):
            result = EthosMemoryBootstrap.for_repo(unpinned_root).run()
        assert result.updated == ["rmh"]
        assert result.vendored_updated == []

    def test_vendored_stale_guides_are_refreshed(self, unpinned_root: Path) -> None:
        identities = _identities(unpinned_root / "global", "rmh")
        vendored = _vendored(unpinned_root, "claude", "rmh")
        repo_subdir = unpinned_root / "src"
        repo_subdir.mkdir()

        with patch("quarry.ethos_memory._GLOBAL_IDENTITIES", identities):
            result = EthosMemoryBootstrap.for_repo(repo_subdir).run()

        assert result.vendored_updated == ["claude", "rmh"]
        for handle in ("claude", "rmh"):
            text = (vendored / f"{handle}.ext" / "quarry.yaml").read_text()
            assert yaml.safe_load(text)["session_context"].startswith(
                MEMORY_GUIDE_HEADER
            )

    def test_vendored_identity_without_ext_stays_without_one(
        self, unpinned_root: Path
    ) -> None:
        identities = _identities(unpinned_root / "global", "rmh")
        vendored = _vendored(unpinned_root, "claude")
        (vendored / "kpz.yaml").write_text("agent: kpz\n")

        result = EthosMemoryBootstrap(identities, vendored=vendored).run()

        assert result.vendored_updated == ["claude"]
        assert not (vendored / "kpz.ext").exists()

    def test_second_run_is_a_no_op(self, unpinned_root: Path) -> None:
        identities = _identities(unpinned_root / "global", "rmh")
        vendored = _vendored(unpinned_root, "claude")

        EthosMemoryBootstrap(identities, vendored=vendored).run()
        result = EthosMemoryBootstrap(identities, vendored=vendored).run()

        assert result.vendored_updated == []

    def test_vendored_refresh_runs_without_a_global_install(
        self, unpinned_root: Path
    ) -> None:
        """A repo-only identity's ext is the vendored one; refresh it regardless."""
        vendored = _vendored(unpinned_root, "claude")

        result = EthosMemoryBootstrap(
            unpinned_root / "no-global", vendored=vendored
        ).run()

        assert result.skipped is True
        assert result.vendored_updated == ["claude"]

    def test_vendored_failure_is_recorded_and_others_refresh(
        self, unpinned_root: Path
    ) -> None:
        identities = _identities(unpinned_root / "global", "rmh")
        vendored = _vendored(unpinned_root, "claude", "kpz")
        (vendored / "kpz.ext" / "quarry.yaml").write_bytes(b"memory_collection: \xff\n")

        result = EthosMemoryBootstrap(identities, vendored=vendored).run()

        assert result.vendored_updated == ["claude"]
        assert "kpz" in result.failed


class TestVendoredSeal:
    """``quarry enable`` on a hostile checkout must not write outside it (CWE-59)."""

    def test_symlinked_vendored_ext_is_refused_not_followed(
        self, unpinned_root: Path
    ) -> None:
        identities = _identities(unpinned_root / "global", "rmh")
        vendored = _vendored(unpinned_root, "claude")
        outside = unpinned_root / "elsewhere" / "quarry.yaml"
        outside.parent.mkdir()
        outside.write_text("memory_collection: memory-claude\n" + _V1_BLOCK)
        before = outside.read_text()
        link = vendored / "claude.ext" / "quarry.yaml"
        link.unlink()
        link.symlink_to(outside)

        result = EthosMemoryBootstrap(identities, vendored=vendored).run()

        assert result.vendored_updated == []
        assert "claude" in result.failed
        assert outside.read_text() == before
        assert link.is_symlink()

    def test_for_repo_seals_at_the_checkout_root(self, unpinned_root: Path) -> None:
        """The whole ``identities`` directory as a link is refused per handle."""
        identities = _identities(unpinned_root / "global", "rmh")
        elsewhere = unpinned_root / "elsewhere" / "identities"
        elsewhere.mkdir(parents=True)
        ext = elsewhere / "claude.ext"
        ext.mkdir()
        target = ext / "quarry.yaml"
        target.write_text("memory_collection: memory-claude\n" + _V1_BLOCK)
        before = target.read_text()
        (unpinned_root / ".punt-labs" / "ethos").mkdir(parents=True)
        (unpinned_root / ".punt-labs" / "ethos" / "identities").symlink_to(elsewhere)

        with patch("quarry.ethos_memory._GLOBAL_IDENTITIES", identities):
            result = EthosMemoryBootstrap.for_repo(unpinned_root).run()

        assert "claude" in result.failed
        assert result.vendored_updated == []
        assert target.read_text() == before
