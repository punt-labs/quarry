"""Guard the storage stack's declared bounds against the lock file."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

if TYPE_CHECKING:
    from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parent.parent

# Storage stack and the first version of the next minor/major it must exclude.
STORAGE_STACK = {"lancedb": "0.41.0", "pyarrow": "27.0.0"}


def _declared(name: str) -> SpecifierSet:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps]
    return next(r.specifier for r in reqs if r.name == name)


def _locked(name: str) -> Version:
    with (ROOT / "uv.lock").open("rb") as fh:
        packages = tomllib.load(fh)["package"]
    return Version(next(p["version"] for p in packages if p["name"] == name))


@pytest.mark.parametrize("name", STORAGE_STACK)
def test_locked_version_satisfies_declared_specifier(name: str) -> None:
    assert _locked(name) in _declared(name)


@pytest.mark.parametrize(("name", "next_release"), STORAGE_STACK.items())
def test_specifier_excludes_next_release(name: str, next_release: str) -> None:
    assert Version(next_release) not in _declared(name)


@pytest.mark.parametrize("name", STORAGE_STACK)
def test_specifier_has_upper_bound(name: str) -> None:
    operators = {spec.operator for spec in _declared(name)}
    assert operators & {"<", "<=", "==", "~="}, f"{name} has no upper bound"
