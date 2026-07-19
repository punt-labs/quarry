"""Tests for CapturesCollection: <repo>-captures derivation and the fallback."""

from __future__ import annotations

from pathlib import Path

from quarry.captures_collection import CapturesCollection


class TestCapturesCollection:
    def test_for_repo(self) -> None:
        assert CapturesCollection.for_repo("quarry").name == "quarry-captures"

    def test_resolve_none_is_default(self) -> None:
        assert CapturesCollection.resolve(None).name == "default-captures"

    def test_fallback_is_default_captures(self) -> None:
        assert CapturesCollection.fallback().name == "default-captures"

    def test_for_cwd_registered_ancestor(self) -> None:
        regs = {"/projects/myapp": "myapp"}
        got = CapturesCollection.for_cwd("/projects/myapp/src/lib", regs)
        assert got.name == "myapp-captures"

    def test_for_cwd_unregistered_is_default(self) -> None:
        got = CapturesCollection.for_cwd("/somewhere/else", {"/x": "x"})
        assert got.name == "default-captures"

    def test_empty_cwd_is_default_not_daemon_cwd(self) -> None:
        """A blank cwd resolves to default-captures, never ``Path("").resolve()``.

        ``Path("").resolve()`` returns the daemon PROCESS's cwd, so without an
        explicit guard a capture with no cwd would misfile into whatever project
        quarryd happened to be launched from.  The registration below maps the
        current process cwd — the guard must short-circuit before it can match.
        """
        regs = {str(Path.cwd()): "the-daemon-project"}
        assert CapturesCollection.for_cwd("", regs).name == "default-captures"
        assert CapturesCollection.for_cwd("   ", regs).name == "default-captures"
