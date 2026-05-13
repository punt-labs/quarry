"""Tests for session artifact extraction."""

from __future__ import annotations

from quarry.artifacts import (
    SessionArtifacts,
    extract_artifacts,
    format_artifacts_frontmatter,
    format_artifacts_header,
)

# -- extract_artifacts: commit SHAs ---------------------------------------


def test_extracts_commit_shas() -> None:
    text = "commit a950c1c some message"
    arts = extract_artifacts(text)
    assert arts.commit_shas == ("a950c1c",)


def test_extracts_long_shas() -> None:
    text = "commit 0dcdbd5abc12 feat: something"
    arts = extract_artifacts(text)
    assert arts.commit_shas == ("0dcdbd5abc12",)


def test_extracts_sha_from_bracket_format() -> None:
    text = "[main abc1234] chore: update deps"
    arts = extract_artifacts(text)
    assert arts.commit_shas == ("abc1234",)


def test_extracts_sha_from_oneline_log() -> None:
    text = "a950c1c Merge pull request #267\n0dcdbd5 chore: update README"
    arts = extract_artifacts(text)
    assert arts.commit_shas == ("a950c1c", "0dcdbd5")


# -- extract_artifacts: PR numbers ----------------------------------------


def test_extracts_pr_numbers() -> None:
    text = "PR #269 merged successfully"
    arts = extract_artifacts(text)
    assert arts.pr_numbers == (269,)


def test_extracts_pr_from_url_context() -> None:
    text = "see pulls/270 for details"
    arts = extract_artifacts(text)
    assert arts.pr_numbers == (270,)


def test_extracts_pr_from_pull_request() -> None:
    text = "Merge pull request #267 from punt-labs/post-release"
    arts = extract_artifacts(text)
    assert arts.pr_numbers == (267,)


def test_extracts_pr_from_merged() -> None:
    text = "merged #123 into main"
    arts = extract_artifacts(text)
    assert arts.pr_numbers == (123,)


def test_ignores_bare_hash_numbers() -> None:
    text = "# Section heading\n## Another heading\nissue #5 in list"
    arts = extract_artifacts(text)
    assert arts.pr_numbers == ()


# -- extract_artifacts: branch names --------------------------------------


def test_extracts_branch_names() -> None:
    text = "git checkout -b fix/status-performance main"
    arts = extract_artifacts(text)
    assert arts.branch_names == ("fix/status-performance",)


def test_extracts_branch_from_push() -> None:
    text = "git push origin feat/backfill"
    arts = extract_artifacts(text)
    assert arts.branch_names == ("feat/backfill",)


def test_extracts_branch_from_push_with_flags() -> None:
    text = "git push -u origin feat/session-artifacts"
    arts = extract_artifacts(text)
    assert arts.branch_names == ("feat/session-artifacts",)


def test_extracts_branch_from_log_decoration() -> None:
    text = "(origin/feat/quarry-recall-claudemd) some commit"
    arts = extract_artifacts(text)
    assert arts.branch_names == ("feat/quarry-recall-claudemd",)


# -- extract_artifacts: bead IDs ------------------------------------------


def test_extracts_bead_ids() -> None:
    text = "bd close quarry-vdh6"
    arts = extract_artifacts(text)
    assert arts.bead_ids == ("quarry-vdh6",)


def test_extracts_bead_from_closes() -> None:
    text = "Closes quarry-nmev"
    arts = extract_artifacts(text)
    assert arts.bead_ids == ("quarry-nmev",)


def test_extracts_bead_from_bead_keyword() -> None:
    text = "bead quarry-abc1 is in progress"
    arts = extract_artifacts(text)
    assert arts.bead_ids == ("quarry-abc1",)


def test_extracts_bead_from_bd_update() -> None:
    text = "bd update biff-0ap --status=in_progress"
    arts = extract_artifacts(text)
    assert arts.bead_ids == ("biff-0ap",)


# -- extract_artifacts: deduplication & edge cases ------------------------


def test_deduplicates() -> None:
    text = "\n".join(["commit a950c1c"] * 5)
    arts = extract_artifacts(text)
    assert arts.commit_shas == ("a950c1c",)


def test_empty_text() -> None:
    arts = extract_artifacts("")
    assert arts.commit_shas == ()
    assert arts.pr_numbers == ()
    assert arts.branch_names == ()
    assert arts.bead_ids == ()


def test_no_false_positives_on_prose() -> None:
    text = (
        "The quick brown fox jumps over the lazy dog. "
        "We discussed the architecture and decided to proceed. "
        "The meeting lasted about 45 minutes. "
        "Total cost was $1,234.56 for the quarter."
    )
    arts = extract_artifacts(text)
    assert arts.commit_shas == ()
    assert arts.pr_numbers == ()
    assert arts.branch_names == ()
    assert arts.bead_ids == ()


def test_mixed_artifacts() -> None:
    text = (
        "commit a950c1c Merge pull request #267\n"
        "git checkout -b feat/session-artifacts main\n"
        "bd close quarry-vdh6\n"
        "PR #269 merged\n"
        "Closes quarry-nmev\n"
    )
    arts = extract_artifacts(text)
    assert "a950c1c" in arts.commit_shas
    assert 267 in arts.pr_numbers
    assert 269 in arts.pr_numbers
    assert "feat/session-artifacts" in arts.branch_names
    assert "quarry-vdh6" in arts.bead_ids
    assert "quarry-nmev" in arts.bead_ids


# -- format_artifacts_header ----------------------------------------------


def test_formats_all_sections() -> None:
    arts = SessionArtifacts(
        commit_shas=("a950c1c", "0dcdbd5"),
        pr_numbers=(269, 270),
        branch_names=("fix/status-performance",),
        bead_ids=("quarry-vdh6",),
    )
    header = format_artifacts_header(arts)
    assert "## Session Artifacts" in header
    assert "Commits: a950c1c, 0dcdbd5" in header
    assert "PRs: #269, #270" in header
    assert "Branches: fix/status-performance" in header
    assert "Beads: quarry-vdh6" in header


def test_omits_empty_sections() -> None:
    arts = SessionArtifacts(
        commit_shas=("a950c1c",),
        pr_numbers=(),
        branch_names=(),
        bead_ids=(),
    )
    header = format_artifacts_header(arts)
    assert "Commits: a950c1c" in header
    assert "PRs:" not in header
    assert "Branches:" not in header
    assert "Beads:" not in header


def test_empty_artifacts_returns_empty() -> None:
    arts = SessionArtifacts(
        commit_shas=(),
        pr_numbers=(),
        branch_names=(),
        bead_ids=(),
    )
    assert format_artifacts_header(arts) == ""


# -- format_artifacts_frontmatter -------------------------------------------


def test_frontmatter_all_fields() -> None:
    arts = SessionArtifacts(
        commit_shas=("a950c1c", "0dcdbd5"),
        pr_numbers=(269, 270),
        branch_names=("fix/status-performance",),
        bead_ids=("quarry-vdh6",),
    )
    fm = format_artifacts_frontmatter("ed821224-abcd", "2026-05-12T21:44:17Z", arts)
    assert fm.startswith("---\n")
    assert fm.endswith("\n---")
    assert "session_id: ed821224-abcd" in fm
    assert 'timestamp: "2026-05-12T21:44:17Z"' in fm
    assert "  - a950c1c" in fm
    assert "  - 269" in fm
    assert "  - fix/status-performance" in fm
    assert "  - quarry-vdh6" in fm


def test_frontmatter_partial() -> None:
    arts = SessionArtifacts(
        commit_shas=("abc1234",),
        pr_numbers=(),
        branch_names=(),
        bead_ids=(),
    )
    fm = format_artifacts_frontmatter("sess-1234", "2026-01-01T00:00:00Z", arts)
    assert "commits:" in fm
    assert "prs:" not in fm
    assert "branches:" not in fm
    assert "beads:" not in fm


def test_frontmatter_empty_artifacts() -> None:
    arts = SessionArtifacts(
        commit_shas=(),
        pr_numbers=(),
        branch_names=(),
        bead_ids=(),
    )
    fm = format_artifacts_frontmatter("sess-1234", "2026-01-01T00:00:00Z", arts)
    assert fm.startswith("---\n")
    assert "session_id: sess-1234" in fm


def test_frontmatter_empty_session_id() -> None:
    arts = SessionArtifacts(
        commit_shas=("abc1234",),
        pr_numbers=(),
        branch_names=(),
        bead_ids=(),
    )
    assert format_artifacts_frontmatter("", "2026-01-01T00:00:00Z", arts) == ""
