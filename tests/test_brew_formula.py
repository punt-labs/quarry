"""Tests for scripts/brew_formula.py, the uv.lock -> Homebrew formula generator."""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "brew_formula.py"
MAIN_URL = "https://files.pythonhosted.org/packages/aa/bb/punt_quarry-9.9.9.tar.gz"
MAIN_SHA = "ab" * 32
OVERRIDES_TEXT = (
    "# GUI build breaks import on headless hosts\n"
    'opencv-python ; sys_platform == "never"\n'
)


def _load() -> ModuleType:
    # Running the script puts scripts/ on sys.path; loading by file path does not.
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("brew_formula", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


bf = _load()
brew_lock = importlib.import_module("brew_lock")  # on sys.path via _load()


def fake_pypi(path: str) -> Mapping[str, Any]:
    """Stand-in for the PyPI JSON API: only the punt-quarry sdist is asked for."""
    version = bf.project_version(ROOT / "pyproject.toml")
    assert path == f"/pypi/punt-quarry/{version}/json"
    return {
        "urls": [
            {"packagetype": "bdist_wheel", "url": "x.whl", "digests": {"sha256": "0"}},
            {"packagetype": "sdist", "url": MAIN_URL, "digests": {"sha256": MAIN_SHA}},
        ]
    }


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    with (ROOT / "uv.lock").open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def overrides(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("overrides") / "install-overrides.txt"
    path.write_text(OVERRIDES_TEXT, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def formula(overrides: Path) -> str:
    return str(
        bf.generate(ROOT / "uv.lock", ROOT / "pyproject.toml", fake_pypi, overrides)
    )


def _resource_blocks(text: str) -> dict[str, str]:
    return {
        m[1]: m[2]
        for m in re.finditer(r'  resource "([^"]+)" do\n(.*?)\n  end\n', text, re.S)
    }


def _locked(lock: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in lock["package"] if p["name"] == name)


def test_main_package_comes_from_pypi_json(formula: str) -> None:
    assert f'  url "{MAIN_URL}"\n  sha256 "{MAIN_SHA}"\n' in formula


def test_formula_depends_on_cmake_to_build_pyarrow_sdist(formula: str) -> None:
    assert (
        '  depends_on "cmake" => :build # pyarrow sdist builds against apache-arrow\n'
        in formula
    )


def test_current_lock_pins_lancedb_and_pyarrow_with_lock_hashes(
    formula: str, lock: dict[str, Any]
) -> None:
    blocks = _resource_blocks(formula)
    arm_wheel = next(
        w
        for w in _locked(lock, "lancedb")["wheels"]
        if w["url"].endswith("macosx_11_0_arm64.whl")
    )
    assert "lancedb-0.40.0-" in blocks["lancedb"]
    assert arm_wheel["hash"].removeprefix("sha256:") in blocks["lancedb"]
    sdist = _locked(lock, "pyarrow")["sdist"]
    assert "pyarrow-26.0.0.tar.gz" in blocks["pyarrow"]
    assert sdist["hash"].removeprefix("sha256:") in blocks["pyarrow"]


def test_every_resource_version_and_hash_equals_the_lock(
    formula: str, lock: dict[str, Any]
) -> None:
    blocks = _resource_blocks(formula)
    assert blocks
    for name, body in blocks.items():
        package = _locked(lock, name)
        urls = re.findall(r'url "([^"]+)"', body)
        hashes = re.findall(r'sha256 "([0-9a-f]{64})"', body)
        assert len(urls) == len(hashes) >= 1
        locked = {
            e["url"]: e["hash"].removeprefix("sha256:")
            for e in ([package["sdist"]] if "sdist" in package else [])
            + package.get("wheels", [])
        }
        for url, digest in zip(urls, hashes, strict=True):
            assert locked[url] == digest, name
            filename = url.rsplit("/", 1)[1]
            version = package["version"]
            assert re.search(rf"[-_]{re.escape(version)}[-.]", filename), name


def test_resources_cover_runtime_closure_and_exclude_dev_and_windows(
    formula: str,
) -> None:
    names = set(_resource_blocks(formula))
    assert {"lancedb", "pyarrow", "mcp", "httpx", "numpy"} <= names
    assert not names & {"pytest", "mypy", "ruff", "colorama", "pywin32", "punt-quarry"}


def test_wheel_only_packages_get_platform_blocks(
    formula: str, lock: dict[str, Any]
) -> None:
    blocks = _resource_blocks(formula)
    wheel_only = {
        p["name"]
        for p in lock["package"]
        if "sdist" not in p and "wheels" in p and p["name"] in blocks
    }
    assert {"lancedb", "onnxruntime"} <= wheel_only
    for name in wheel_only | {"pymupdf", "tree-sitter-c-sharp", "tree-sitter-yaml"}:
        body = blocks[name]
        if name == "flatbuffers" or "on_macos" not in body:
            continue  # pure-Python wheel-only package: one plain url
        for marker in ("on_macos do", "on_linux do", "on_arm do", "on_intel do"):
            assert marker in body, (name, marker)
        # Intel macOS is unsupported: no macosx x86_64 wheel is ever emitted.
        assert "x86_64.whl" not in body.split("on_linux do")[0], name
        assert "macosx_10_15_x86_64" not in body, name


def test_pure_python_wheel_only_package_is_a_plain_resource(formula: str) -> None:
    body = _resource_blocks(formula)["flatbuffers"]
    assert "on_macos" not in body
    assert "none-any.whl" in body


def test_install_block_without_list_matches_wheel_set(formula: str) -> None:
    wheel_names = sorted(
        n for n, body in _resource_blocks(formula).items() if "on_macos" in body
    )
    assert wheel_names[:2] == ["lancedb", "onnxruntime"]
    listed = "\n".join(f"      {n}" for n in wheel_names)
    assert f"    wheel_names = %w[\n{listed}\n    ]\n" in formula
    assert "without: wheel_names + %w[hf-xet]" in formula
    assert "hf-xet" in _resource_blocks(formula)
    assert 'assert_match version.to_s, shell_output("#{bin}/quarry version")' in formula


def test_plain_resources_are_sorted(formula: str) -> None:
    tail = formula.split("# other runtime dependency in uv.lock.")[1]
    names = list(_resource_blocks(tail))
    assert names == sorted(names)


def test_generation_is_deterministic(overrides: Path) -> None:
    first = bf.generate(ROOT / "uv.lock", ROOT / "pyproject.toml", fake_pypi, overrides)
    second = bf.generate(
        ROOT / "uv.lock", ROOT / "pyproject.toml", fake_pypi, overrides
    )
    assert first == second
    assert first.endswith("end\n")


def test_main_writes_output_file_and_stdout(
    tmp_path: Path, overrides: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "quarry.rb"
    args = ["--overrides", str(overrides)]
    assert bf.main([*args, "--output", str(out)], fake_pypi) == 0
    assert out.read_text(encoding="utf-8").startswith("# typed: false")
    assert bf.main(args, fake_pypi) == 0
    assert capsys.readouterr().out == out.read_text(encoding="utf-8")


def test_main_fails_loudly_when_pypi_has_no_sdist(
    overrides: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_sdist(path: str) -> Mapping[str, Any]:
        return {"urls": [{"packagetype": "bdist_wheel"}]}

    assert bf.main(["--overrides", str(overrides)], no_sdist) == 1
    assert "no sdist" in capsys.readouterr().err


def test_main_fails_loudly_when_lock_is_missing(
    tmp_path: Path, overrides: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--lock", str(tmp_path / "absent.lock"), "--overrides", str(overrides)]
    assert bf.main(argv, fake_pypi) == 1
    assert "brew-formula" in capsys.readouterr().err


def test_missing_platform_wheel_is_an_error(tmp_path: Path, overrides: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
[[package]]
name = "punt-quarry"
version = "1.0.0"
dependencies = [{ name = "solo" }]

[[package]]
name = "solo"
version = "1.0"
wheels = [
    { url = "https://h/solo-1.0-cp310-abi3-win_amd64.whl", hash = "sha256:aa" },
]
""",
        encoding="utf-8",
    )
    with pytest.raises(bf.FormulaError, match="no macOS arm64 wheel"):
        bf.generate(lock, ROOT / "pyproject.toml", fake_pypi, overrides)


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (None, True),
        ("sys_platform == 'win32'", False),
        ("sys_platform != 'emscripten'", True),
        ("sys_platform == 'emscripten' or sys_platform == 'win32'", False),
        ("platform_machine == 'arm64' and sys_platform == 'darwin'", True),
        ("platform_machine == 'AMD64' or platform_machine == 'x86_64'", True),
        ("implementation_name != 'PyPy'", True),
    ],
)
def test_marker_holds(*, marker: str | None, expected: bool) -> None:
    assert brew_lock.marker_holds(marker) is expected


def test_unsupported_marker_is_an_error() -> None:
    with pytest.raises(bf.FormulaError, match="unsupported"):
        brew_lock.marker_holds("python_version >= '3.9'")


def test_overridden_package_is_excluded_and_headless_stays(tmp_path: Path) -> None:
    overrides = tmp_path / "install-overrides.txt"
    overrides.write_text(
        "# GUI build breaks import on headless hosts\n"
        'opencv-python ; sys_platform == "never"\n',
        encoding="utf-8",
    )
    text = bf.generate(ROOT / "uv.lock", ROOT / "pyproject.toml", fake_pypi, overrides)
    names = set(_resource_blocks(text))
    assert "opencv-python" not in names
    assert "opencv-python-headless" in names
    assert "opencv-python\n" not in text
    assert "      opencv-python-headless\n" in text


def test_missing_overrides_file_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "absent.txt"
    with pytest.raises(bf.FormulaError, match="overrides file not found"):
        bf.InstallOverrides.from_file(absent)
    with pytest.raises(bf.FormulaError, match=r"absent\.txt"):
        bf.generate(ROOT / "uv.lock", ROOT / "pyproject.toml", fake_pypi, absent)
    assert bf.main(["--overrides", str(absent)], fake_pypi) == 1
    assert "absent.txt" in capsys.readouterr().err


def test_only_never_true_markers_are_overrides(tmp_path: Path) -> None:
    overrides = tmp_path / "o.txt"
    overrides.write_text(
        "numpy>=2 \nfoo ; sys_platform == 'never'\n"
        'Bar_Baz ; sys_platform == "never"\n'
        "qux ; sys_platform == 'linux'\n",
        encoding="utf-8",
    )
    assert bf.InstallOverrides.from_file(overrides).excluded == {"foo", "bar-baz"}


def test_dropped_resource_missing_from_closure_is_an_error(
    tmp_path: Path, overrides: Path
) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
[[package]]
name = "punt-quarry"
version = "1.0.0"
dependencies = [{ name = "plain" }]

[[package]]
name = "plain"
version = "1.0"
sdist = { url = "https://h/plain-1.0.tar.gz", hash = "sha256:aa" }
""",
        encoding="utf-8",
    )
    with pytest.raises(bf.FormulaError, match="drops hf-xet"):
        bf.generate(lock, ROOT / "pyproject.toml", fake_pypi, overrides)


def test_without_list_is_derived_from_dropped_resources(
    monkeypatch: pytest.MonkeyPatch, overrides: Path
) -> None:
    monkeypatch.setattr(bf, "DROPPED_RESOURCES", ("hf-xet", "certifi"))
    text = bf.generate(ROOT / "uv.lock", ROOT / "pyproject.toml", fake_pypi, overrides)
    assert "without: wheel_names + %w[certifi hf-xet]" in text
    assert "@@" not in text
