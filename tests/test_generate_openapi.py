"""Tests for the OpenAPI generator's output-path resolution."""

from __future__ import annotations

import tools.generate_openapi as gen


def test_output_path_is_repo_anchored_not_cwd() -> None:
    """_OUTPUT resolves from the module, so the caller's CWD never retargets it."""
    out = gen._OUTPUT
    assert out.is_absolute()
    assert out.parent.name == "docs"
    assert out.name == "openapi.json"
    # Anchored at the repo root (the dir holding pyproject.toml), not the CWD.
    assert (out.parent.parent / "pyproject.toml").is_file()
