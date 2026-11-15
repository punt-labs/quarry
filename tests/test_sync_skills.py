"""Tests for ``tools/sync_skills.py``, the plugin/skills <-> _skills mirror."""

from __future__ import annotations

from pathlib import Path

import tools.sync_skills as sync_skills
from tools.sync_skills import SkillsSync


def _write_skill(root: Path, name: str, body: str = "hello") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n{body}\n", encoding="utf-8"
    )
    references = skill_dir / "references"
    references.mkdir()
    (references / "detail.md").write_text("detail\n", encoding="utf-8")


def test_module_constants_resolve_to_the_real_repo_tree() -> None:
    """``_SOURCE``/``_PACKAGED`` are repo-anchored, not CWD-relative."""
    assert sync_skills._SOURCE.is_dir()
    assert sync_skills._SOURCE.name == "skills"
    assert sync_skills._SOURCE.parent.name == "plugin"
    assert sync_skills._PACKAGED.parent.name == "quarry"
    assert sync_skills._PACKAGED.name == "_skills"


def test_the_real_committed_mirror_is_current() -> None:
    """The committed ``src/quarry/_skills/`` must already match ``plugin/skills/``.

    Guards against an edit to ``plugin/skills/`` landing without its
    ``make sync-skills`` companion commit.
    """
    assert SkillsSync(sync_skills._SOURCE, sync_skills._PACKAGED).is_current()


class TestSkillsSync:
    def test_sync_copies_every_file_byte_for_byte(self, tmp_path: Path) -> None:
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a")
        packaged = tmp_path / "pkg" / "_skills"
        sync = SkillsSync(source, packaged)

        sync.sync()

        assert (packaged / "demo-a" / "SKILL.md").read_text(encoding="utf-8") == (
            source / "demo-a" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert (packaged / "demo-a" / "references" / "detail.md").is_file()

    def test_is_current_false_before_any_sync(self, tmp_path: Path) -> None:
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a")
        packaged = tmp_path / "pkg" / "_skills"

        assert SkillsSync(source, packaged).is_current() is False

    def test_is_current_true_immediately_after_sync(self, tmp_path: Path) -> None:
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a")
        packaged = tmp_path / "pkg" / "_skills"
        sync = SkillsSync(source, packaged)
        sync.sync()

        assert sync.is_current() is True

    def test_a_source_symlink_round_trips_sync_then_check(self, tmp_path: Path) -> None:
        """A skill containing a symlink syncs and then passes --check (PL-1049
        #1): the copy preserves the link so it matches the hash, instead of
        materializing the target and failing the check right after a sync."""
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a")
        (source / "demo-a" / "link.md").symlink_to("SKILL.md")
        packaged = tmp_path / "pkg" / "_skills"
        sync = SkillsSync(source, packaged)
        sync.sync()

        copied = packaged / "demo-a" / "link.md"
        assert copied.is_symlink()
        assert sync.is_current() is True

    def test_is_current_false_after_source_content_changes(
        self, tmp_path: Path
    ) -> None:
        """A content edit must be caught even when the file's size is unchanged
        -- a shallow ``os.stat``-based comparison would miss this."""
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a", body="original")
        packaged = tmp_path / "pkg" / "_skills"
        sync = SkillsSync(source, packaged)
        sync.sync()

        (source / "demo-a" / "SKILL.md").write_text(
            "---\nname: demo-a\ndescription: test\n---\n\nchanged!!\n",
            encoding="utf-8",
        )

        assert sync.is_current() is False

    def test_is_current_false_after_a_file_is_added_to_source(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a")
        packaged = tmp_path / "pkg" / "_skills"
        sync = SkillsSync(source, packaged)
        sync.sync()

        _write_skill(source, "demo-b")

        assert sync.is_current() is False

    def test_sync_is_idempotent_and_replaces_stale_content(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a", body="v1")
        packaged = tmp_path / "pkg" / "_skills"
        sync = SkillsSync(source, packaged)
        sync.sync()

        (source / "demo-a" / "SKILL.md").write_text(
            "---\nname: demo-a\ndescription: test\n---\n\nv2\n", encoding="utf-8"
        )
        sync.sync()

        assert "v2" in (packaged / "demo-a" / "SKILL.md").read_text(encoding="utf-8")
        assert sync.is_current() is True

    def test_main_check_fails_on_drift_and_passes_once_synced(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        source = tmp_path / "plugin" / "skills"
        _write_skill(source, "demo-a")
        packaged = tmp_path / "pkg" / "_skills"
        monkeypatch.setattr(sync_skills, "_SOURCE", source)  # type: ignore[attr-defined]
        monkeypatch.setattr(sync_skills, "_PACKAGED", packaged)  # type: ignore[attr-defined]

        assert sync_skills.main(["--check"]) == 1

        sync_skills.main([])

        assert sync_skills.main(["--check"]) == 0
