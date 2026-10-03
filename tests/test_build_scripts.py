"""Tests for the release/build shell scripts under ``scripts/``.

These guard the Claude Desktop ``.mcpb`` build and the release-verify scripts.
The ``.mcpb`` distribution broke silently once (quarry-521g): ``build-mcpb.sh``
read a ``manifest.json`` that had been removed, no CI job built the bundle, and
no release ever attached the asset — so the README download link 404'd. Shell
scripts also had no shellcheck coverage beyond ``install.sh``, which is part of
how the broken ``build-mcpb.sh`` stayed broken.

The checks here are static plus shellcheck (no ``node``/``mcpb`` needed) so they
run in the same CI as the rest of the suite; the live bundle build is exercised
locally and in the release workflow, which installs ``mcpb``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
# The shippable plugin surface — what a marketplace install materializes. It is
# also the plugin root ``${CLAUDE_PLUGIN_ROOT}`` names at runtime.
PLUGIN_ROOT = REPO_ROOT / "plugin"
BUILD_MCPB = SCRIPTS_DIR / "build-mcpb.sh"
MANIFEST_TEMPLATE = SCRIPTS_DIR / "mcpb-manifest.template.json"
README_SHA_CHECK = SCRIPTS_DIR / "check-readme-install-sha.sh"
# Every module that registers MCP tools via ``add_tool`` — split across
# sibling classes (ResourceCatalog, DocumentTools, MissionTools) so no single
# module holds every tool, per the 500-line module cap.
MCP_TOOL_MODULES = tuple(
    REPO_ROOT / "src" / "quarry" / name
    for name in (
        "mcp_server.py",
        "mcp_catalog.py",
        "mcp_documents.py",
        "mcp_missions.py",
    )
)

# The bundle invokes ``quarry mcp`` — the stdio client of quarryd (DES-031 v2.2).
# It ships the BARE command ``quarry`` (not an absolute path) on purpose: the
# mcpb runtime launches the server from the login-shell PATH, where
# ``uv tool install`` places the ``quarry`` binary (e.g. ~/.local/bin), so a bare
# command resolves. This deliberately differs from ``quarry install``'s manual
# claude_desktop_config.json wiring, which runs under a minimal PATH and so must
# write a ``shutil.which``-resolved ABSOLUTE path (doctor.py resolve_paths=True).
# Same subcommand, different launcher — do not "align" the bundle to an absolute
# path; that would break the mcpb runtime, which has no build-time absolute path.
EXPECTED_MCP_COMMAND = "quarry"
EXPECTED_MCP_ARGS = ["mcp"]


def _shell_scripts() -> list[Path]:
    """Every shell script this repo owns: build/release, installer, and hooks.

    The hook scripts are included because they are the only shell that runs on a
    *user's* machine on every session — the highest-consequence scripts here and
    the last ones to gain coverage.
    """
    return [
        *sorted(SCRIPTS_DIR.glob("*.sh")),
        REPO_ROOT / "install.sh",
        *sorted((PLUGIN_ROOT / "hooks").glob("*.sh")),
    ]


def _registered_tool_names() -> set[str]:
    """Wire names the MCP server registers, parsed from every ``register()``.

    A tool registered with ``name="x"`` uses that wire name; otherwise the
    bound method name is the wire name (FastMCP's default). Tools are spread
    across :data:`MCP_TOOL_MODULES`, not just ``mcp_server.py``.
    """
    pattern = re.compile(r'add_tool\(\s*self\.(\w+)\s*(?:,\s*name="(\w+)")?\s*\)')
    names: set[str] = set()
    for module in MCP_TOOL_MODULES:
        for method, override in pattern.findall(module.read_text()):
            names.add(override or method)
    return names


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_shell_script_passes_shellcheck(script: Path) -> None:
    """Every shell script this repo ships or runs passes shellcheck.

    Per CLAUDE.md Class 5 / testing rule 6: shell scripts must pass
    ``shellcheck -x``. Extending this beyond install.sh closes the gap that let
    a broken build-mcpb.sh ship unlinted.
    """
    shellcheck_bin = shutil.which("shellcheck")
    if shellcheck_bin is None:
        pytest.fail(
            "shellcheck is required for shell-script linting but was not found "
            "on PATH. Install it in CI (apt-get install shellcheck) so this "
            "gate cannot be skipped."
        )
    result = subprocess.run(
        [shellcheck_bin, "-x", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck failed on {script.name}:\n{result.stdout}\n{result.stderr}"
    )


def test_build_mcpb_does_not_read_root_manifest() -> None:
    """build-mcpb.sh must not depend on a repo-root manifest.json.

    b2c9ffb removed the manifest.json that sat beside the plugin manifest,
    because the marketplace preferred it over .claude-plugin/plugin.json and
    stripped the plugin's slash commands. The manifest is generated into a
    staging dir instead; a reference to a repo-root manifest would resurrect the
    broken build.
    """
    script = BUILD_MCPB.read_text()
    assert "open('manifest.json')" not in script
    assert "json.load(open('manifest.json'))" not in script
    assert "mcpb pack ." not in script, "must pack from staging, not the repo root"


def test_build_mcpb_stages_and_sources_version_from_pyproject() -> None:
    script = BUILD_MCPB.read_text()
    assert "dist/mcpb-staging" in script, "manifest must be staged, not at repo root"
    assert "pyproject.toml" in script, "version must come from pyproject.toml"
    assert "dist/punt-quarry.mcpb" in script, "stable-named copy must be produced"


def test_no_manifest_json_in_plugin_root() -> None:
    """A manifest.json in the plugin root re-breaks plugin slash commands.

    The guard follows the plugin root, which is ``plugin/`` — the only directory
    a git-subdir install materializes and therefore the only one the marketplace
    reads a manifest from. A stray manifest.json at the repo root no longer
    reaches a user; one inside ``plugin/`` would.
    """
    assert not (PLUGIN_ROOT / "manifest.json").exists(), (
        "manifest.json must not exist in the plugin root (b2c9ffb): the plugin "
        "marketplace would prefer it over .claude-plugin/plugin.json and drop "
        "the plugin's slash commands. Generate it into dist/mcpb-staging."
    )


def test_manifest_template_has_version_placeholder() -> None:
    template = json.loads(MANIFEST_TEMPLATE.read_text())
    assert template["version"] == "__VERSION__", (
        "template version must be a placeholder filled from pyproject at build "
        "time (PL-DI-5: pyproject is the single source of truth)"
    )


def test_manifest_template_mcp_config_invokes_bare_quarry_mcp() -> None:
    """The bundle's mcp_config must invoke the bare ``quarry mcp`` command.

    The mcpb runtime resolves ``quarry`` from the login-shell PATH (where
    ``uv tool install`` puts the binary), so the command is bare, not an absolute
    path. This is intentionally NOT the same as ``quarry install``'s manual
    claude_desktop_config.json wiring, which writes a ``shutil.which``-resolved
    absolute path for its minimal-PATH runtime (doctor.py resolve_paths=True).
    """
    template = json.loads(MANIFEST_TEMPLATE.read_text())
    mcp_config = template["server"]["mcp_config"]
    assert mcp_config["command"] == EXPECTED_MCP_COMMAND
    assert mcp_config["args"] == EXPECTED_MCP_ARGS


def test_manifest_template_reflects_current_daemon_data_dir() -> None:
    template = json.loads(MANIFEST_TEMPLATE.read_text())
    assert "~/.punt-labs/quarry" in template["long_description"], (
        "manifest must reflect the current daemon data dir, not the stale "
        "~/.quarry/data"
    )
    assert "~/.quarry/data" not in json.dumps(template), "stale data dir present"


def test_manifest_template_tools_match_registered_mcp_tools() -> None:
    """The manifest tool list must equal the MCP server's registered wire names.

    A tool added to (or removed from) the server without updating the manifest
    is a surface-drift bug — the "every surface or none" invariant. Cross-check
    the two so drift fails a test.
    """
    template = json.loads(MANIFEST_TEMPLATE.read_text())
    manifest_tools = {tool["name"] for tool in template["tools"]}
    assert manifest_tools == _registered_tool_names()


def _run_git(repo: Path, *args: str) -> str:
    """Run git in *repo* with a fixed identity and no signing (fixture setup)."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_fixture_repo(
    repo: Path, install_text: str, install_name: str = "install.sh"
) -> str:
    """Commit *install_name* into a fresh git repo; return its short SHA.

    Hermetic: the fixture never contains a3c10f9 / 6f90f11, so these tests do not
    depend on the live repo's history or clone depth — the CI shallow-clone that
    made a pinned SHA absent from the object DB and broke the previous, non-
    hermetic version of these tests. *install_name* lets a test point both sides
    of the check at a non-default installer path.
    """
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    (repo / install_name).write_text(install_text)
    _run_git(repo, "add", install_name)
    _run_git(repo, "commit", "-q", "-m", f"add {install_name}")
    return _run_git(repo, "rev-parse", "--short", "HEAD")


def _run_check(
    repo: Path, install_path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the check against fixture *repo* via the REPO_DIR override.

    When *install_path* is given it is passed as INSTALL_PATH, so the test can
    confirm the override drives BOTH the git-show read and the working-tree read.
    """
    env = {**os.environ, "REPO_DIR": str(repo)}
    if install_path is not None:
        env["INSTALL_PATH"] = install_path
    return subprocess.run(
        ["bash", str(README_SHA_CHECK)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _pin(sha: str, path: str = "install.sh") -> str:
    url = f"https://raw.githubusercontent.com/punt-labs/quarry/{sha}/{path}"
    return f"curl -fsSL {url} | sh\n"


def test_readme_install_sha_check_passes_when_pin_matches(tmp_path: Path) -> None:
    """A README pinning a SHA whose install.sh equals the tree's passes."""
    repo = tmp_path / "repo"
    sha = _make_fixture_repo(repo, "#!/bin/sh\necho install v1\n")
    (repo / "README.md").write_text(_pin(sha))
    result = _run_check(repo)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_readme_install_sha_check_rejects_stale_pin(tmp_path: Path) -> None:
    """A pin whose install.sh differs from the tree is stale and must fail.

    install.sh moves on (v1 → v2) but the README pin still points at the v1
    commit — the exact stale-SHA class the check guards against.
    """
    repo = tmp_path / "repo"
    sha = _make_fixture_repo(repo, "#!/bin/sh\necho install v1\n")
    (repo / "README.md").write_text(_pin(sha))
    (repo / "install.sh").write_text("#!/bin/sh\necho install v2\n")
    result = _run_check(repo)
    assert result.returncode != 0, "stale README install SHA must be rejected"
    assert "stale" in result.stderr.lower()


def test_readme_install_sha_check_reports_absent_sha(tmp_path: Path) -> None:
    """A pin absent from the object DB (shallow clone) fails with a clear message.

    This is the CI condition that broke the prior tests: the pinned commit is not
    in the local object DB. The script must say so plainly, not pass silently.
    """
    repo = tmp_path / "repo"
    _make_fixture_repo(repo, "#!/bin/sh\necho install v1\n")
    (repo / "README.md").write_text(_pin("0" * 40))
    result = _run_check(repo)
    assert result.returncode != 0, "an absent pinned SHA must fail"
    assert "not present in the local" in result.stderr.lower()


def test_readme_install_sha_check_reports_no_install_url(tmp_path: Path) -> None:
    """A README with no pinned install URL hits the empty-check, not a crash.

    Under `set -o pipefail` the extraction pipeline's final `grep` exits non-zero
    on no match; without the `|| true` guard the script would abort there and
    skip the intended "no install-URL SHA found" message. Assert the clean
    empty-check path, not a pipefail termination.
    """
    repo = tmp_path / "repo"
    _make_fixture_repo(repo, "#!/bin/sh\necho install v1\n")
    (repo / "README.md").write_text("# Quarry\n\nNo install URL here.\n")
    result = _run_check(repo)
    assert result.returncode != 0, "a README with no install URL must fail"
    assert "no install-url sha found" in result.stderr.lower()


def test_readme_install_sha_check_honors_install_path_on_both_sides(
    tmp_path: Path,
) -> None:
    """INSTALL_PATH drives both the git-show read and the working-tree read.

    Commit a NON-default installer name and run with INSTALL_PATH set to it. If
    the git-show side still hard-coded install.sh, `git show <sha>:install.sh`
    would fail (no such path in the commit); passing proves both reads use the
    same resolved path.
    """
    repo = tmp_path / "repo"
    sha = _make_fixture_repo(
        repo, "#!/bin/sh\necho install v1\n", install_name="installer.sh"
    )
    (repo / "README.md").write_text(_pin(sha, path="installer.sh"))
    result = _run_check(repo, install_path="installer.sh")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
