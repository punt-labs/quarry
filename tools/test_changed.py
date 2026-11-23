"""Choose default-tier tests from the staged Git paths."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path


def _imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imported


def select_tests(root: Path, staged: set[Path]) -> list[Path] | None:
    """Return paths to run, or None for the whole default tier."""
    shared = {
        Path("Makefile"),
        Path("conftest.py"),
        Path("pyproject.toml"),
        Path("tools/test_changed.py"),
        Path("tools/test_quarantine.py"),
        Path("uv.lock"),
    }
    if staged & shared:
        return None
    if any(path.parts[0] in {".githooks", ".github", "tools"} for path in staged):
        return None
    source = {path for path in staged if path.is_relative_to("src/quarry")}
    changed_tests = {path for path in staged if path.is_relative_to("tests")}
    tests = {
        path
        for path in changed_tests
        if path.match("test_*.py") and (root / path).is_file()
    }
    if changed_tests - tests or any(
        path.suffix != ".py" or path.name == "__init__.py" for path in source
    ):
        return None
    if source:
        modules = {".".join(path.with_suffix("").parts[1:]) for path in source}
        matched: set[str] = set()
        for path in (root / "tests").rglob("test_*.py"):
            direct = _imports(path) & modules
            if direct:
                matched.update(direct)
                tests.add(path.relative_to(root))
        if matched != modules:
            return None
    if any(
        path.is_relative_to("scripts") or path == Path("install.sh") for path in staged
    ):
        script_tests = {
            path.relative_to(root) for path in (root / "tests").glob("test_*script*.py")
        }
        if not script_tests:
            return None
        tests.update(script_tests)
    return sorted(tests)


def main() -> int:
    """Run the affected subset with pytest's default marker filter."""
    result = subprocess.check_output(
        ["git", "diff", "--cached", "--no-renames", "--name-only", "-z"]
    )
    selected = select_tests(
        Path.cwd(), {Path(os.fsdecode(name)) for name in result.split(b"\0") if name}
    )
    if selected == []:
        return 0
    command = [sys.executable, "-m", "pytest"]
    result_code = subprocess.call([*command, *(str(path) for path in selected or [])])
    if selected is not None and result_code == 5:
        # Only opt-in cases were affected. The default gate still has to run.
        return subprocess.call(command)
    return result_code


if __name__ == "__main__":
    sys.exit(main())
