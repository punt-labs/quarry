"""EmbeddingModel: whether the model cache needs the home directory to be found."""

from __future__ import annotations

from pathlib import Path

import pytest
from huggingface_hub import hf_hub_download

from quarry.doctor_model import EmbeddingModel
from quarry.doctor_preconditions import Need
from quarry.embeddings import OnnxEmbeddingBackend
from quarry.home_paths import QuarryHome
from quarry.model_cache import ONNX_MODEL_REPO, ONNX_MODEL_REVISION, ModelCache
from quarry.service_environment import SERVICE_LABEL, ServiceEnvironment
from quarry.shutdown_policy import ShutdownPolicy


@pytest.fixture(autouse=True)
def _cache_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.mark.parametrize(
    ("cache", "expected"),
    [
        ("/srv/hf/hub", Need.NONE),
        ("~/.cache/huggingface/hub", Need.HOME),
        (".cache/huggingface/hub", Need.HOME),
    ],
)
def test_the_cache_needs_the_home_only_when_it_is_not_an_absolute_path(
    cache: str, expected: Need, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", cache)
    assert EmbeddingModel.needs() == expected


def test_override_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, subpath, expected in (
        ("XDG_CACHE_HOME", "xdg", "xdg/huggingface/hub"),
        ("HF_HOME", "hf", "hf/hub"),
        ("HUGGINGFACE_HUB_CACHE", "old-hub", "old-hub"),
        ("HF_HUB_CACHE", "hub", "hub"),
    ):
        monkeypatch.setenv(name, f"~/{subpath}")
        assert ModelCache.root() == tmp_path / expected


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("override", [False, True])
def test_doctor_checks_the_cache_saved_by_managed_install(
    system: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, override: bool
) -> None:
    monkeypatch.setattr("quarry.service_environment.platform.system", lambda: system)
    cache = QuarryHome.cache_root()
    if override:
        cache = tmp_path / 'cache & "models"'
        monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    if system == "Linux":
        unit = QuarryHome.systemd_unit()
        unit.parent.mkdir(parents=True)
        unit.touch()
        ServiceEnvironment.write()
        if not override:
            assert f'HF_HUB_CACHE="{cache}"' in QuarryHome.env_file().read_text()
    else:
        plist = QuarryHome.launchd_plist(SERVICE_LABEL)
        plist.parent.mkdir(parents=True)
        entries = ServiceEnvironment.launchd_entries("", ShutdownPolicy.from_settings())
        if not override:
            assert f"<string>{cache}</string>" in entries
        plist.write_text(
            f'<plist version="1.0"><dict><key>EnvironmentVariables</key>'
            f"<dict>{entries}</dict></dict></plist>"
        )
    if override:
        monkeypatch.delenv("HF_HUB_CACHE")
    else:
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "unrelated-shell-cache"))
    model = cache / ("models--" + ONNX_MODEL_REPO.replace("/", "--"))
    snapshot = model / "snapshots" / ONNX_MODEL_REVISION
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "onnx/model_int8.onnx").write_text("model")
    (snapshot / "tokenizer.json").write_text("tokenizer")

    assert EmbeddingModel.check().passed


def test_doctor_reports_invalid_managed_service_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quarry.service_environment.platform.system", lambda: "Linux")
    unit = QuarryHome.systemd_unit()
    unit.parent.mkdir(parents=True)
    unit.touch()
    config = QuarryHome.env_file()
    config.parent.mkdir(parents=True)
    config.write_text('HF_HUB_CACHE="/unclosed\n')

    result = EmbeddingModel.check()

    assert not result.passed
    assert "cannot read installed service settings" in result.message
    assert "/unclosed" not in result.message


def test_doctor_ignores_commented_systemd_cache_assignments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("quarry.service_environment.platform.system", lambda: "Linux")
    unit = QuarryHome.systemd_unit()
    unit.parent.mkdir(parents=True)
    unit.touch()
    config = QuarryHome.env_file()
    config.parent.mkdir(parents=True)
    cache = tmp_path / "cache;models"
    config.write_text(
        f'HF_HUB_CACHE="{cache}"\n'
        '; HF_HUB_CACHE="/not-active"\n'
        '# HF_HUB_CACHE="/also-not-active"\n'
    )

    assert ServiceEnvironment.installed_cache() == cache


@pytest.mark.parametrize("override", [False, True])
def test_download_local_reuse_and_doctor_share_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, override: bool
) -> None:
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    legacy = tmp_path / ".cache" / "huggingface" / "hub"
    legacy.mkdir(parents=True)
    (legacy / "existing").write_text("keep")
    expected = tmp_path / ".punt-labs" / "quarry" / "cache"
    if override:
        expected = tmp_path / "custom-hub"
        monkeypatch.setenv("HF_HUB_CACHE", str(expected))
    repo = "models--" + ONNX_MODEL_REPO.replace("/", "--")

    def fetch(
        *,
        repo_id: str,
        filename: str,
        revision: str,
        cache_dir: Path,
        local_files_only: bool,
    ) -> str:
        assert cache_dir == expected
        assert repo_id == ONNX_MODEL_REPO
        assert revision == ONNX_MODEL_REVISION
        if local_files_only:
            return hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                revision=revision,
                cache_dir=cache_dir,
                local_files_only=True,
            )
        path = cache_dir / repo / "snapshots" / revision / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(filename)
        return str(path)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", fetch)
    assert not EmbeddingModel.check().passed
    paths = OnnxEmbeddingBackend.download_model_files()
    assert OnnxEmbeddingBackend._load_local_model_files("onnx/model_int8.onnx") == paths
    assert EmbeddingModel.check().passed
    assert list(legacy.iterdir()) == [legacy / "existing"]
    assert (legacy / "existing").read_text() == "keep"
