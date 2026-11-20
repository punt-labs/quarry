"""The uv.lock model behind scripts/brew_formula.py.

Reads uv.lock into artifacts, wheels, and resources, resolves the runtime
dependency closure, and evaluates the environment markers that decide which
dependency edges a Homebrew user can reach.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Must match `depends_on "python@3.13"` in the formula template.
PYTHON_TAG = "cp313"
PYTHON_MINOR = 13

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


class FormulaError(RuntimeError):
    """The lock, the pyproject, or PyPI cannot yield a complete formula."""


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

    def runtime_closure(
        self, root: str, excluded: frozenset[str] = frozenset()
    ) -> list[str]:
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
                if (
                    dep_name not in excluded
                    and marker_holds(dep.get("marker"))
                    and dep_name not in seen
                ):
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
