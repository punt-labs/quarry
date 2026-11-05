#!/usr/bin/env python3
"""Render the Homebrew formula (Formula/quarry.rb) from uv.lock and pyproject.toml.

Why this exists: the tap formula's resource stanzas used to come from
``brew update-python-resources``, which resolves the newest PyPI versions
instead of the versions quarry tested and locked, and no release step reran
it.  This generator makes the lock the single source: every resource is the
locked version with the lock's own url and sha256.

Inputs
    uv.lock         resource versions, urls, hashes, the dependency graph
    pyproject.toml  the punt-quarry version
    PyPI JSON API   the punt-quarry sdist url/sha256 (the project itself is an
                    editable entry in the lock, so it has no sdist there)

Output is deterministic: resources are sorted by name and nothing in it
depends on the clock, the environment, or dict ordering, so the same lock and
the same PyPI answers produce byte-identical text.

Usage
    python scripts/brew_formula.py [--output PATH]   (or: make brew-formula)
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brew_lock import (
    Artifact,
    FormulaError,
    Lock,
    PlatformWheels,
    Resource,
    Wheel,
)

ROOT = Path(__file__).resolve().parent.parent
PROJECT_NAME = "punt-quarry"

# Wheels the formula pins even though the lock also carries an sdist, with the
# reason as it appears in the formula.  Packages with no sdist in the lock and
# only platform wheels (lancedb, onnxruntime) are detected from the lock and
# need no entry here.
PINNED_WHEEL_COMMENTS: dict[str, str] = {
    "pymupdf": """\
pymupdf vendors and builds its own full MuPDF + Tesseract + a dozen
third-party C libraries from source when built from sdist -- one of the
heaviest, most fragile source builds in the Python ecosystem, and one
PyMuPDF itself doesn't expect anyone to do (its own docs assume the
wheel). Pin the wheel here rather than fight the from-source build the
way lancedb/onnxruntime are handled above.""",
    "tree-sitter-c-sharp": """\
tree-sitter-c-sharp's sdist build compiles parser.c but not its external
scanner.c, so the built extension dlopens successfully yet crashes on
first real use: "symbol not found ... _tree_sitter_c_sharp_external_
scanner_create". Caught by actually running `quarry doctor`, not
`brew test` -- doctor imports the code_extractor path that pulls this
in. Wheel resource sidesteps the broken sdist build entirely.""",
    "tree-sitter-yaml": """\
Same external-scanner-not-compiled bug as tree-sitter-c-sharp above --
tree-sitter-embedded-template has no external scanner and built fine
from sdist, but yaml does and hits the identical dlopen symbol failure.""",
}

# Resources the formula declares but never installs (see the hf-xet comment in
# the template's install block).  Each must be in the resolved closure.
DROPPED_RESOURCES: tuple[str, ...] = ("hf-xet",)

_NEVER = re.compile(r"^sys_platform\s*==\s*'never'$")

Fetcher = Callable[[str], Mapping[str, Any]]


def pypi_json(path: str) -> Mapping[str, Any]:
    """GET https://pypi.org<path> and decode the JSON body."""
    conn = http.client.HTTPSConnection("pypi.org", timeout=30)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise FormulaError(f"PyPI GET {path} returned HTTP {resp.status}")
    finally:
        conn.close()
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise FormulaError(f"PyPI GET {path} returned a non-object body")
    return doc


class ResourceBuilder:
    """Turns a locked package into a Resource."""

    def __init__(self, lock: Lock) -> None:
        self._lock = lock

    def build(self, name: str) -> Resource:
        version = self._lock.version(name)
        sdist = self._lock.sdist(name)
        if sdist is None or name in PINNED_WHEEL_COMMENTS:
            return self._from_wheels(name, version)
        return Resource(name, version, artifact=sdist)

    def _from_wheels(self, name: str, version: str) -> Resource:
        wheels = self._lock.wheels(name)
        universal = [w for w in wheels if w.is_universal()]
        if universal and name not in PINNED_WHEEL_COMMENTS:
            # Pure-Python package published wheel-only: one plain resource.
            return Resource(name, version, artifact=universal[0].artifact)
        picks = (
            self._pick(name, wheels, lambda w: w.is_macos_arm(), "macOS arm64"),
            self._pick(name, wheels, lambda w: w.is_linux("aarch64"), "Linux arm64"),
            self._pick(name, wheels, lambda w: w.is_linux("x86_64"), "Linux x86_64"),
        )
        return Resource(name, version, wheels=PlatformWheels(*picks))

    @staticmethod
    def _pick(
        name: str,
        wheels: list[Wheel],
        wanted: Callable[[Wheel], bool],
        label: str,
    ) -> Artifact:
        for wheel in wheels:  # sorted by filename: deterministic choice
            if wanted(wheel):
                return wheel.artifact
        raise FormulaError(f"{name}: no {label} wheel for CPython 3.13 in uv.lock")


def sdist_from_pypi(fetch: Fetcher, name: str, version: str) -> Artifact:
    """The sdist url/sha256 of ``name==version`` from the PyPI JSON API."""
    doc = fetch(f"/pypi/{name}/{version}/json")
    for entry in doc.get("urls", []):
        if entry.get("packagetype") == "sdist":
            return Artifact(str(entry["url"]), str(entry["digests"]["sha256"]))
    raise FormulaError(f"PyPI lists no sdist for {name}=={version}")


TEMPLATE = ROOT / "scripts" / "brew_formula.rb.tmpl"


def _comment_block(text: str) -> str:
    return "\n".join(f"  # {line}" for line in text.splitlines())


class FormulaRenderer:
    """Fills scripts/brew_formula.rb.tmpl with the main package and resources."""

    def __init__(self, main: Artifact, resources: list[Resource]) -> None:
        self._main = main
        self._resources = sorted(resources, key=lambda r: r.name)

    def _dropped_names(self) -> list[str]:
        """The resources the formula declares but never installs.

        Every name is checked against the resolved closure: Homebrew raises
        ArgumentError at install time for a ``without:`` entry that is not a
        defined resource, so a relock that drops one must fail here instead.
        """
        defined = {r.name for r in self._resources}
        missing = sorted(set(DROPPED_RESOURCES) - defined)
        if missing:
            raise FormulaError(
                f"formula drops {', '.join(missing)} but no such resource is "
                "in the resolved closure; update DROPPED_RESOURCES"
            )
        return sorted(DROPPED_RESOURCES)

    def render(self) -> str:
        wheels = [r for r in self._resources if r.is_wheel_pinned]
        plain = [r for r in self._resources if not r.is_wheel_pinned]
        text = TEMPLATE.read_text(encoding="utf-8")
        fills = {
            "@@MAIN_URL@@": self._main.url,
            "@@MAIN_SHA256@@": self._main.sha256,
            "@@WHEEL_RESOURCES@@": "".join(self._wheel_block(r) for r in wheels),
            "@@PLAIN_RESOURCES@@": "".join(f"{r.render()}\n\n" for r in plain),
            "@@WHEEL_NAMES@@": "\n".join(f"      {r.name}" for r in wheels),
            "@@DROPPED_NAMES@@": " ".join(self._dropped_names()),
        }
        for marker, value in fills.items():
            text = text.replace(marker, value)
        return text.replace("\n\n\n", "\n\n")

    @staticmethod
    def _wheel_block(resource: Resource) -> str:
        comment = PINNED_WHEEL_COMMENTS.get(resource.name)
        head = f"\n{_comment_block(comment)}\n" if comment else "\n"
        return f"{head}{resource.render()}\n"


def project_version(pyproject: Path) -> str:
    with pyproject.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


@dataclass(frozen=True)
class InstallOverrides:
    """The packages install.sh's override file pins to a never-true marker.

    The file uses uv's override syntax, ``name ; sys_platform == "never"``.
    Any other override is ignored here: the formula installs the lock's
    version, which a version override does not change.
    """

    excluded: frozenset[str]

    @classmethod
    def from_file(cls, path: Path) -> InstallOverrides:
        """Parse ``path``; a missing file is an error, never "no overrides".

        Silently treating a missing file as empty would let the formula keep
        packages (the GUI opencv build) that install.sh deliberately drops.
        """
        if not path.is_file():
            raise FormulaError(f"overrides file not found: {path}")
        names: set[str] = set()
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            name, _, marker = line.partition(";")
            if _NEVER.match(marker.strip().replace('"', "'")):
                names.add(name.strip().lower().replace("_", "-"))
        return cls(frozenset(names))


def generate(
    lock_path: Path,
    pyproject_path: Path,
    fetch: Fetcher,
    overrides_path: Path,
) -> str:
    """Render the formula text for the lock at ``lock_path``."""
    lock = Lock.load(lock_path)
    main = sdist_from_pypi(fetch, PROJECT_NAME, project_version(pyproject_path))
    builder = ResourceBuilder(lock)
    overrides = InstallOverrides.from_file(overrides_path)
    names = lock.runtime_closure(PROJECT_NAME, overrides.excluded)
    return FormulaRenderer(main, [builder.build(n) for n in names]).render()


def main(argv: list[str] | None = None, fetch: Fetcher = pypi_json) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output", type=Path, help="write the formula here (default: stdout)"
    )
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument(
        "--overrides",
        type=Path,
        default=ROOT / "install-overrides.txt",
        help="install.sh override file (required to exist)",
    )
    args = parser.parse_args(argv)
    try:
        text = generate(args.lock, args.pyproject, fetch, args.overrides)
    except (FormulaError, OSError, KeyError, ValueError) as exc:
        sys.stderr.write(f"brew-formula: {exc!r}\n")
        return 1
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
