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

ROOT = Path(__file__).resolve().parent.parent
PROJECT_NAME = "punt-quarry"

# Must match `depends_on "python@3.13"` in the formula template below.
PYTHON_TAG = "cp313"
PYTHON_MINOR = 13

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

# Environments a Homebrew user can run on; a dependency edge is kept when its
# marker holds in at least one of them.  Intel macOS is excluded: unsupported.
TARGET_ENVIRONMENTS: tuple[dict[str, str], ...] = (
    {
        "sys_platform": "darwin",
        "platform_machine": "arm64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    },
    {
        "sys_platform": "linux",
        "platform_machine": "aarch64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    },
    {
        "sys_platform": "linux",
        "platform_machine": "x86_64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    },
)

_CLAUSE = re.compile(r"^\s*(\w+)\s*(==|!=)\s*'([^']*)'\s*$")
_WHEEL_NAME = re.compile(
    r"^(?P<dist>[^-]+)-(?P<ver>[^-]+)(?:-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)

Fetcher = Callable[[str], Mapping[str, Any]]


class FormulaError(RuntimeError):
    """The lock, the pyproject, or PyPI cannot yield a complete formula."""


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


def marker_holds(marker: str | None) -> bool:
    """True when ``marker`` holds in any target environment (None always holds).

    Supports the ``var ==/!= 'value'`` clauses joined by ``and``/``or`` that
    uv.lock emits for the variables in TARGET_ENVIRONMENTS; anything else is an
    error, never a silent include or exclude.
    """
    if marker is None:
        return True
    return any(_eval_marker(marker, env) for env in TARGET_ENVIRONMENTS)


def _eval_marker(marker: str, env: Mapping[str, str]) -> bool:
    def clause(text: str) -> bool:
        m = _CLAUSE.match(text)
        if m is None or m.group(1) not in env:
            raise FormulaError(f"unsupported environment marker clause: {text!r}")
        equal = env[m.group(1)] == m.group(3)
        return equal if m.group(2) == "==" else not equal

    return any(
        all(clause(part) for part in alternative.split(" and "))
        for alternative in marker.split(" or ")
    )


@dataclass(frozen=True)
class Artifact:
    """One downloadable file: its url and sha256."""

    url: str
    sha256: str


@dataclass(frozen=True)
class Wheel:
    """A lock wheel entry with its filename tags parsed."""

    artifact: Artifact
    filename: str
    python_tag: str
    abi_tag: str
    platform_tag: str

    @classmethod
    def from_lock(cls, entry: Mapping[str, Any]) -> Wheel | None:
        url = str(entry["url"])
        filename = url.rsplit("/", 1)[1]
        m = _WHEEL_NAME.match(filename)
        if m is None:
            return None
        return cls(
            Artifact(url, _sha256(entry)),
            filename,
            m["py"],
            m["abi"],
            m["plat"],
        )

    def fits_python(self) -> bool:
        """CPython 3.13 can load it (exact tag, stable ABI, or pure Python)."""
        if self.abi_tag == "none":
            return any(t.startswith("py3") for t in self.python_tag.split("."))
        if self.abi_tag == "abi3":
            return (
                self.python_tag.startswith("cp3")
                and int(self.python_tag[3:]) <= PYTHON_MINOR
            )
        return self.python_tag == PYTHON_TAG and self.abi_tag == PYTHON_TAG

    def is_universal(self) -> bool:
        return self.abi_tag == "none" and self.platform_tag == "any"

    def _platforms(self) -> list[str]:
        return self.platform_tag.split(".")

    def is_macos_arm(self) -> bool:
        return any(
            p.startswith("macosx_") and p.endswith("_arm64") for p in self._platforms()
        )

    def is_linux(self, machine: str) -> bool:
        return any(
            p.startswith("manylinux") and p.endswith(f"_{machine}")
            for p in self._platforms()
        )


def _sha256(entry: Mapping[str, Any]) -> str:
    digest = str(entry["hash"])
    if not digest.startswith("sha256:"):
        raise FormulaError(f"unsupported hash algorithm in lock entry: {digest!r}")
    return digest.removeprefix("sha256:")


@dataclass(frozen=True)
class PlatformWheels:
    """The wheel for each platform Homebrew supports (no Intel macOS)."""

    macos_arm: Artifact
    linux_arm: Artifact
    linux_intel: Artifact

    def render(self) -> list[str]:
        return [
            "    on_macos do",
            *_url_lines(self.macos_arm, "      "),
            "    end",
            "    on_linux do",
            "      on_arm do",
            *_url_lines(self.linux_arm, "        "),
            "      end",
            "      on_intel do",
            *_url_lines(self.linux_intel, "        "),
            "      end",
            "    end",
        ]


@dataclass(frozen=True)
class Resource:
    """A formula resource: one plain artifact or per-platform wheels."""

    name: str
    version: str
    artifact: Artifact | None = None  # None exactly when ``wheels`` is set
    wheels: PlatformWheels | None = None

    @property
    def is_wheel_pinned(self) -> bool:
        return self.wheels is not None

    def render(self) -> str:
        lines = [f'  resource "{self.name}" do']
        if self.wheels is not None:
            lines += self.wheels.render()
        elif self.artifact is not None:
            lines += _url_lines(self.artifact, "    ")
        else:
            raise FormulaError(f"{self.name}: resource has no artifact")
        lines.append("  end")
        return "\n".join(lines)


def _url_lines(artifact: Artifact, indent: str) -> list[str]:
    return [f'{indent}url "{artifact.url}"', f'{indent}sha256 "{artifact.sha256}"']


class Lock:
    """The parts of uv.lock the formula needs."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._packages: dict[str, Mapping[str, Any]] = {
            str(p["name"]): p for p in document["package"]
        }

    @classmethod
    def load(cls, path: Path) -> Lock:
        with path.open("rb") as handle:
            return cls(tomllib.load(handle))

    def runtime_closure(self, root: str) -> list[str]:
        """Sorted names reachable from ``root`` over non-optional edges.

        Edges whose marker never holds on a supported platform (colorama on
        win32, pywin32, emscripten shims) are dropped.  An edge with ``extra``
        also pulls that extra's optional dependencies.
        """
        seen: set[str] = set()
        stack = [root]
        while stack:
            name = stack.pop()
            package = self._package(name)
            edges = list(package.get("dependencies", []))
            for dep in package.get("dependencies", []):
                extras = dep.get("extra", [])
                optional = self._package(str(dep["name"])).get(
                    "optional-dependencies", {}
                )
                for extra in extras:
                    edges.extend(optional.get(extra, []))
            for dep in edges:
                dep_name = str(dep["name"])
                if marker_holds(dep.get("marker")) and dep_name not in seen:
                    seen.add(dep_name)
                    stack.append(dep_name)
        seen.discard(root)
        return sorted(seen)

    def _package(self, name: str) -> Mapping[str, Any]:
        try:
            return self._packages[name]
        except KeyError:
            raise FormulaError(f"{name} is absent from uv.lock") from None

    def version(self, name: str) -> str:
        return str(self._package(name)["version"])

    def sdist(self, name: str) -> Artifact | None:
        entry = self._package(name).get("sdist")
        if entry is None:
            return None
        return Artifact(str(entry["url"]), _sha256(entry))

    def wheels(self, name: str) -> list[Wheel]:
        parsed = (Wheel.from_lock(w) for w in self._package(name).get("wheels", []))
        return sorted(
            (w for w in parsed if w is not None and w.fits_python()),
            key=lambda w: w.filename,
        )


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


def generate(lock_path: Path, pyproject_path: Path, fetch: Fetcher) -> str:
    """Render the formula text for the lock at ``lock_path``."""
    lock = Lock.load(lock_path)
    main = sdist_from_pypi(fetch, PROJECT_NAME, project_version(pyproject_path))
    builder = ResourceBuilder(lock)
    names = lock.runtime_closure(PROJECT_NAME)
    return FormulaRenderer(main, [builder.build(n) for n in names]).render()


def main(argv: list[str] | None = None, fetch: Fetcher = pypi_json) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output", type=Path, help="write the formula here (default: stdout)"
    )
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    args = parser.parse_args(argv)
    try:
        text = generate(args.lock, args.pyproject, fetch)
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
