# Build Plan: Provider Auto-Detection (DES-016)

Spec for rmh. Implements provider auto-detection with all kpz review
findings incorporated.

## Acceptance Criteria

1. `select_provider()` returns a `ProviderSelection` dataclass -- no session.
2. `OnnxEmbeddingBackend.__init__` owns session creation with explicit
   `ORT_ENABLE_ALL` session options.
3. `QUARRY_PROVIDER` env var supports three values: `cpu`, `cuda`, unset.
   `cuda` forces CUDA and raises on failure. Unknown values raise `ValueError`.
4. `provider.py` has zero imports from `quarry.embeddings` or `huggingface_hub`.
5. All 830+ existing tests pass. 8 new tests in `tests/test_provider.py`.
6. `make check` green (ruff, mypy, pyright, pytest).

## File-by-File Spec

### NEW: `src/quarry/provider.py` (~50 lines)

```python
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROVIDER_MODEL_MAP: dict[str, str] = {
    "CUDAExecutionProvider": "onnx/model_fp16.onnx",
    "CPUExecutionProvider": "onnx/model_int8.onnx",
}

@dataclass(frozen=True)
class ProviderSelection:
    provider: str       # e.g. "CUDAExecutionProvider"
    model_file: str     # e.g. "onnx/model_fp16.onnx" (HF repo-relative path)

def select_provider() -> ProviderSelection:
    """Detect the best ONNX Runtime execution provider.

    Reads QUARRY_PROVIDER env var. Probes ort.get_available_providers().
    Falls back to CPU when CUDA is unavailable.

    Returns ProviderSelection -- never raises for hardware issues.
    Raises ValueError for unknown QUARRY_PROVIDER values.
    """
```

**Implementation rules:**

1. Lazy-import `onnxruntime as ort` inside the function body.

2. Read `os.environ.get("QUARRY_PROVIDER")`. Normalize to lowercase.
   - `None` / empty string: auto-detect (continue to step 3).
   - `"cpu"`: log INFO `"Provider override: cpu (QUARRY_PROVIDER)"`,
     return `ProviderSelection("CPUExecutionProvider", "onnx/model_int8.onnx")`.
   - `"cuda"`: set `force_cuda = True`, continue to step 3 but skip
     fallback on failure (raise instead).
   - Anything else: raise `ValueError(f"Unknown QUARRY_PROVIDER value: {value!r}. Expected 'cpu', 'cuda', or unset.")`.

3. Call `ort.get_available_providers()`. If `"CUDAExecutionProvider"` is
   in the list, return `ProviderSelection("CUDAExecutionProvider", "onnx/model_fp16.onnx")`.

   **Do NOT create a session here.** Session validation moves to
   `OnnxEmbeddingBackend.__init__` (see below). The provider module only
   answers "what provider and precision?" based on what ORT reports and
   the env var. The actual CUDA validation (session creation) happens in
   embeddings.py where the model path is resolved.

4. If CUDA is not in the list and `force_cuda` is True, raise
   `RuntimeError("QUARRY_PROVIDER=cuda but CUDAExecutionProvider not available")`.

5. CPU fallback: log INFO `"Using CPUExecutionProvider + int8"`, return
   `ProviderSelection("CPUExecutionProvider", "onnx/model_int8.onnx")`.

**No other functions. No other imports.** The module must not import
anything from `quarry.embeddings`, `quarry.config`, or `huggingface_hub`.

### MODIFY: `src/quarry/config.py`

Remove the `ONNX_MODEL_FILE` constant (line 17). It is replaced by
`PROVIDER_MODEL_MAP` in `provider.py`.

Keep all other constants unchanged:
- `ONNX_MODEL_REPO`
- `ONNX_MODEL_REVISION`
- `ONNX_TOKENIZER_FILE`
- `ONNX_QUERY_PREFIX`

### MODIFY: `src/quarry/embeddings.py`

**Imports:** Remove `ONNX_MODEL_FILE` from the config import. Add:
```python
from quarry.provider import select_provider
```

**`download_model_files(model_file: str = "onnx/model_int8.onnx")`:**
Add `model_file` parameter with default. Replace the hardcoded
`ONNX_MODEL_FILE` reference with the parameter. Signature:
```python
def download_model_files(model_file: str = "onnx/model_int8.onnx") -> tuple[str, str]:
```

**`_load_local_model_files(model_file: str)`:**
Add required `model_file` parameter. Replace `ONNX_MODEL_FILE` with it.
```python
def _load_local_model_files(model_file: str) -> tuple[str, str]:
```

**`_load_model_files(model_file: str)`:**
Add required `model_file` parameter. Pass it through to
`_load_local_model_files(model_file)` and `download_model_files(model_file)`.
Update the download size in the log message from "~500 MB" to "~120-220 MB"
(depends on model variant).
```python
def _load_model_files(model_file: str) -> tuple[str, str]:
```

**`OnnxEmbeddingBackend.__init__`:** Replace the current init with:

```python
def __init__(self) -> None:
    self._dimension = 768

    selection = select_provider()

    model_path, tokenizer_path = _load_model_files(selection.model_file)

    from tokenizers import Tokenizer  # noqa: PLC0415

    logger.info("Loading ONNX embedding model: %s", ONNX_MODEL_REPO)
    self._tokenizer = Tokenizer.from_file(tokenizer_path)
    self._tokenizer.enable_padding()
    self._tokenizer.enable_truncation(max_length=512)

    import onnxruntime as ort  # noqa: PLC0415

    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = (
        ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    )

    try:
        self._session = ort.InferenceSession(
            model_path,
            sess_options=sess_options,
            providers=[selection.provider],
        )
        logger.info(
            "ONNX model loaded: provider=%s, model=%s",
            selection.provider,
            selection.model_file,
        )
    except Exception:
        if selection.provider == "CUDAExecutionProvider":
            logger.warning(
                "CUDA session failed, falling back to CPU + int8",
                exc_info=True,
            )
            cpu_model_file = "onnx/model_int8.onnx"
            model_path, _ = _load_model_files(cpu_model_file)
            self._session = ort.InferenceSession(
                model_path,
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )
            logger.info(
                "ONNX model loaded: provider=CPUExecutionProvider, model=%s",
                cpu_model_file,
            )
        else:
            raise
```

This is the CUDA validation that the design originally put in
`provider.py`. Moving it here means:
- `provider.py` stays pure (no model loading, no HF, no sessions).
- The actual CUDA validation happens with the real model path
  (resolved by `_load_model_files`).
- If CUDA fails (cuDNN missing, driver mismatch, FP16 model not
  downloaded), it falls back to CPU + int8 with a WARNING log.
- If `QUARRY_PROVIDER=cuda` was set, `select_provider()` already
  returned CUDA. The fallback here respects that by re-raising
  only when the provider is not CUDA (i.e., CPU failures are
  always fatal). For forced CUDA (`QUARRY_PROVIDER=cuda`), add
  a check: if the env var is `cuda`, re-raise instead of falling back.

Revised exception block:
```python
    except Exception:
        force_cuda = (
            os.environ.get("QUARRY_PROVIDER", "").lower() == "cuda"
        )
        if selection.provider == "CUDAExecutionProvider" and not force_cuda:
            logger.warning(
                "CUDA session failed, falling back to CPU + int8",
                exc_info=True,
            )
            cpu_model_file = "onnx/model_int8.onnx"
            model_path, _ = _load_model_files(cpu_model_file)
            self._session = ort.InferenceSession(
                model_path,
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )
            logger.info(
                "ONNX model loaded: provider=CPUExecutionProvider, model=%s",
                cpu_model_file,
            )
        else:
            raise
```

Add `import os` to the top-level imports.

### MODIFY: `src/quarry/doctor.py`

**`_check_embedding_model()`** (line 85-120): Replace reference to
`ONNX_MODEL_FILE` with the string literal `"onnx/model_int8.onnx"`.
This function checks whether the int8 model is cached -- it should
always check int8 regardless of provider. Remove `ONNX_MODEL_FILE`
from the import on line 89.

```python
from quarry.config import (
    ONNX_MODEL_REPO,
    ONNX_MODEL_REVISION,
    ONNX_TOKENIZER_FILE,
)
```

Replace `ONNX_MODEL_FILE` usage on lines 96 and 97 with the string
`"onnx/model_int8.onnx"`.

**`run_install()`** step 2 (line 499-504): After downloading the int8
model, optionally download the FP16 model if CUDA is available. Add
after the existing `download_model_files()` call:

```python
# Also download FP16 model if CUDA is available
try:
    import onnxruntime as ort  # noqa: PLC0415
    if "CUDAExecutionProvider" in ort.get_available_providers():
        download_model_files(model_file="onnx/model_fp16.onnx")
        print("  \u2713 FP16 model cached (for CUDA)")  # noqa: T201
except Exception:  # noqa: BLE001
    pass  # FP16 download is optional -- first-use fallback works
```

### MODIFY: `tests/test_embeddings.py`

**Update `_patch_onnx_backend`** to also patch `select_provider`:

```python
def _patch_onnx_backend(session: MagicMock, tokenizer: MagicMock):
    """Patch provider selection, model loading, tokenizer, and ORT."""
    from quarry.provider import ProviderSelection

    return (
        patch(
            "quarry.embeddings._load_model_files",
            return_value=("/fake/model.onnx", "/fake/tokenizer.json"),
        ),
        patch("tokenizers.Tokenizer.from_file", return_value=tokenizer),
        patch("onnxruntime.InferenceSession", return_value=session),
        patch(
            "quarry.embeddings.select_provider",
            return_value=ProviderSelection(
                provider="CPUExecutionProvider",
                model_file="onnx/model_int8.onnx",
            ),
        ),
    )
```

**Update all call sites** that unpack `_patch_onnx_backend` as 3 patches
to unpack 4. Every `p1, p2, p3 = _patch_onnx_backend(...)` becomes
`p1, p2, p3, p4 = _patch_onnx_backend(...)` and every `with p1, p2, p3:`
becomes `with p1, p2, p3, p4:`.

**Update `TestAutoDownloadFallback`**: The `_load_model_files` function
now takes a `model_file` parameter. Update the mock calls to expect it:

```python
class TestAutoDownloadFallback:
    def test_uses_local_when_cached(self):
        with (
            patch(
                "quarry.embeddings._load_local_model_files",
                return_value=("/cached/model.onnx", "/cached/tokenizer.json"),
            ) as local_mock,
            patch("quarry.embeddings.download_model_files") as download_mock,
        ):
            result = _load_model_files("onnx/model_int8.onnx")

        assert result == ("/cached/model.onnx", "/cached/tokenizer.json")
        local_mock.assert_called_once_with("onnx/model_int8.onnx")
        download_mock.assert_not_called()

    def test_downloads_when_not_cached(self):
        with (
            patch(
                "quarry.embeddings._load_local_model_files",
                side_effect=OSError("not cached"),
            ),
            patch(
                "quarry.embeddings.download_model_files",
                return_value=("/downloaded/model.onnx", "/downloaded/tokenizer.json"),
            ) as download_mock,
        ):
            result = _load_model_files("onnx/model_int8.onnx")

        assert result == ("/downloaded/model.onnx", "/downloaded/tokenizer.json")
        download_mock.assert_called_once_with("onnx/model_int8.onnx")
```

### NEW: `tests/test_provider.py`

All tests mock `onnxruntime.get_available_providers`. No real ORT calls.

## Test Spec

### `tests/test_provider.py`

| # | Test name | Setup | Assertion |
|---|-----------|-------|-----------|
| 1 | `test_cpu_only_returns_cpu_int8` | Mock `get_available_providers` -> `["CPUExecutionProvider"]`. No env var. | Returns `ProviderSelection("CPUExecutionProvider", "onnx/model_int8.onnx")` |
| 2 | `test_cuda_available_returns_cuda_fp16` | Mock `get_available_providers` -> `["CUDAExecutionProvider", "CPUExecutionProvider"]`. No env var. | Returns `ProviderSelection("CUDAExecutionProvider", "onnx/model_fp16.onnx")` |
| 3 | `test_env_cpu_overrides_cuda` | Mock `get_available_providers` -> `["CUDAExecutionProvider", "CPUExecutionProvider"]`. `monkeypatch.setenv("QUARRY_PROVIDER", "cpu")`. | Returns `ProviderSelection("CPUExecutionProvider", "onnx/model_int8.onnx")` |
| 4 | `test_env_cuda_with_cuda_available` | Mock `get_available_providers` -> `["CUDAExecutionProvider", "CPUExecutionProvider"]`. `monkeypatch.setenv("QUARRY_PROVIDER", "cuda")`. | Returns `ProviderSelection("CUDAExecutionProvider", "onnx/model_fp16.onnx")` |
| 5 | `test_env_cuda_without_cuda_raises` | Mock `get_available_providers` -> `["CPUExecutionProvider"]`. `monkeypatch.setenv("QUARRY_PROVIDER", "cuda")`. | Raises `RuntimeError` with message containing "CUDAExecutionProvider not available" |
| 6 | `test_env_unknown_value_raises` | `monkeypatch.setenv("QUARRY_PROVIDER", "rocm")`. | Raises `ValueError` with message containing "Unknown QUARRY_PROVIDER" |
| 7 | `test_cpu_override_logs_info` | Mock `get_available_providers` -> `["CPUExecutionProvider"]`. `monkeypatch.setenv("QUARRY_PROVIDER", "cpu")`. Use `caplog` at INFO level. | `"Provider override: cpu (QUARRY_PROVIDER)"` appears in `caplog.text` |
| 8 | `test_empty_providers_returns_cpu` | Mock `get_available_providers` -> `[]`. No env var. | Returns `ProviderSelection("CPUExecutionProvider", "onnx/model_int8.onnx")`. (Edge case: ORT installed without any EP. Probe falls through to CPU fallback.) |

**Mock pattern for all tests:**
```python
with patch("onnxruntime.get_available_providers", return_value=[...]):
    result = select_provider()
```

Import `select_provider` and `ProviderSelection` from `quarry.provider`.

## Build Sequence

Each step must end with `make check` green before proceeding.

### Step 1: Create `src/quarry/provider.py`

Write the module with `select_provider()`, `ProviderSelection`,
`PROVIDER_MODEL_MAP`. No other modules change yet.

Verify: `make check` passes (new module has no callers yet, but must
pass lint/type checks).

### Step 2: Create `tests/test_provider.py`

Write all 8 tests. They should all pass because `select_provider()`
is self-contained (only depends on `onnxruntime.get_available_providers`
which is mocked).

Verify: `make check` passes, `uv run pytest tests/test_provider.py -v`
shows 8 passed.

### Step 3: Modify `config.py` -- remove `ONNX_MODEL_FILE`

Delete the constant. This will break `embeddings.py` and `doctor.py`
imports. Fix them in the same step:

- `embeddings.py`: Remove `ONNX_MODEL_FILE` from import. Add
  `model_file` parameter to `download_model_files`,
  `_load_model_files`, `_load_local_model_files`. Add
  `from quarry.provider import select_provider`. Rewrite `__init__`
  per the spec above. Add `import os`.

- `doctor.py`: Remove `ONNX_MODEL_FILE` from import. Replace with
  string literal `"onnx/model_int8.onnx"` in `_check_embedding_model`.

- `tests/test_embeddings.py`: Update `_patch_onnx_backend` to return
  4 patches (add `select_provider` mock). Update all call sites to
  unpack 4. Update `TestAutoDownloadFallback` to pass `model_file` arg.

All changes in this step are coupled -- they must land together or
nothing compiles.

Verify: `make check` passes, full test suite green.

### Step 4: Add FP16 download to `doctor.py` `run_install()`

Add the optional FP16 download block after the int8 download in step 2
of `run_install()`.

Verify: `make check` passes.

### Step 5: Benchmark `ORT_ENABLE_ALL` vs default

Run the existing benchmark with and without `ORT_ENABLE_ALL` on CPU.
Record times. If `ORT_ENABLE_ALL` shows no regression, keep it (it is
already in the code from step 3). If it regresses, remove it and
document why.

This step produces data, not code. Log the results in a comment on
the PR.

## What NOT to Do

- **Do not add CoreML support.** Benchmark data (0.8 texts/s, 99
  partitions) rules it out.
- **Do not change the `EmbeddingBackend` protocol** in `types.py`.
- **Do not change `backends.py`.** The double-checked lock and cache
  are unchanged.
- **Do not add thread count configuration** (`inter_op_num_threads`,
  `intra_op_num_threads`). Defaults are fine for single-user. Out of
  scope.
- **Do not change batch size.** GPU batch tuning is a separate
  optimization.
- **Do not add a `QUARRY_MODEL` env var.** Model selection is derived
  from provider, not user-configurable.
- **Do not create a throwaway probe session.** The production session
  IS the validation.
- **Do not import `huggingface_hub` in `provider.py`.** Model path
  resolution stays in `embeddings.py`.
- **Do not re-export `ONNX_MODEL_FILE` or add a compatibility shim.**
  The constant is dead. All callers are updated in step 3.
