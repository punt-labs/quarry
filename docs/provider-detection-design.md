# Provider Auto-Detection and Model Selection

Design for DES-016 implementation. Adds runtime provider probing, model
precision selection, and graceful fallback to `quarry`'s embedding layer.

> **Note:** This document is the original design proposal. The review at
> `provider-detection-review.md` identified structural changes (drop session
> from ProviderSelection, support QUARRY_PROVIDER=cuda). The build plan at
> `build-plan-quarry-b9m.md` is the authoritative implementation spec and
> supersedes this document where they differ.

## 1. Provider Probe Algorithm

The probe runs once per process, at `OnnxEmbeddingBackend.__init__` time
(which is itself cached in `backends.py`). It does not run at import time.

```
def _select_provider() -> tuple[str, str]:
    """Return (provider_name, model_file) for the best available config.

    Probes ONNX Runtime's available providers, then validates the top
    candidate by creating a throwaway InferenceSession. If the candidate
    fails (e.g. CUDA listed but cuDNN missing), falls back to CPU.
    """
```

### Probe steps

1. Read `QUARRY_PROVIDER` env var. If set to `"cpu"`, short-circuit to
   `("CPUExecutionProvider", "onnx/model_int8.onnx")`. Log at INFO:
   `"Provider override: QUARRY_PROVIDER=cpu, using CPUExecutionProvider + int8"`.

2. Call `ort.get_available_providers()`. This returns a list like
   `["CUDAExecutionProvider", "CPUExecutionProvider"]`.

3. If `"CUDAExecutionProvider"` is in the list, attempt validation (step 4).
   Otherwise, skip to step 5.

4. **CUDA validation**: create a minimal `InferenceSession` with the FP16
   model file and `providers=["CUDAExecutionProvider"]`. Wrap in
   try/except. If the session creates successfully, CUDA is confirmed.
   If it raises (missing cuDNN, driver mismatch, model not downloaded yet),
   log a WARNING with the exception message and fall through to step 5.

   Note: this validation creates a real session with the production model,
   not a throwaway. The session is returned and reused as the production
   session. This avoids creating two sessions (one probe + one real).

5. **CPU fallback**: return `("CPUExecutionProvider", "onnx/model_int8.onnx")`.
   Log at INFO: `"Using CPUExecutionProvider + int8"`.

### Why not probe CoreML

DES-016 benchmarks show CoreML is a dead end (0.8 texts/s, 99 graph
partitions). It is excluded from the probe entirely. If Apple ships a
Neural Engine-friendly ONNX EP in the future, we can revisit with new
benchmark data.

### Why validate with a real session

`ort.get_available_providers()` lies. It reports `CUDAExecutionProvider`
as available even when cuDNN is missing, the driver version is wrong, or
the GPU has insufficient memory. The only reliable test is creating a
session. Since we need the session anyway, we keep it.

## 2. Model Selection Mapping

One dict, no user configuration:

```python
PROVIDER_MODEL_MAP: dict[str, str] = {
    "CUDAExecutionProvider": "onnx/model_fp16.onnx",
    "CPUExecutionProvider": "onnx/model_int8.onnx",
}
```

| Provider | Model file | Size | Throughput |
|----------|-----------|------|------------|
| CUDAExecutionProvider | `onnx/model_fp16.onnx` | ~218 MB | 3,042 texts/s (RTX 5080) |
| CPUExecutionProvider | `onnx/model_int8.onnx` | ~120 MB | 9.4 texts/s (M2), 134 texts/s (AMD) |

All variants produce 768-dim FP32 vectors. The `np.asarray(..., dtype=np.float32)`
cast in `embed_texts` already handles this.

## 3. Fallback Chain

Ordered, each step either succeeds (return) or falls through:

```
1. QUARRY_PROVIDER=cpu       -> CPU + int8          (explicit override)
2. CUDA available + valid    -> CUDA + FP16         (best throughput)
3. CUDA available + invalid  -> log WARNING, fall through
4. CPU (always available)    -> CPU + int8           (universal fallback)
```

No step raises an exception. The function always returns a working
(provider, model_file) pair. The only way to fail is if
`CPUExecutionProvider` itself is broken, which means onnxruntime is not
installed — and that's caught earlier by the import.

## 4. File Changes

### `src/quarry/provider.py` — NEW (~80 lines)

New module. Contains:

- `PROVIDER_MODEL_MAP` — the dict above
- `ProviderSelection` — a frozen dataclass with `provider: str`,
  `model_file: str`, `session: ort.InferenceSession`
- `select_provider() -> ProviderSelection` — the probe algorithm from
  section 1. Returns a validated session ready to use.
- No public mutable state. The function is pure (aside from the env var
  read and hardware probe).

Why a new file: `config.py` is for static settings. `embeddings.py` is
for the embedding backend. Provider detection is its own concern —
hardware probing, fallback logic, logging. Mixing it into either
existing file would violate single responsibility.

### `src/quarry/config.py` — MODIFY

- Remove `ONNX_MODEL_FILE` constant (replaced by `PROVIDER_MODEL_MAP`)
- Keep `ONNX_MODEL_REPO`, `ONNX_MODEL_REVISION`, `ONNX_TOKENIZER_FILE`,
  `ONNX_QUERY_PREFIX` unchanged

### `src/quarry/embeddings.py` — MODIFY

- `_load_model_files()` gains a `model_file: str` parameter (instead of
  reading the global `ONNX_MODEL_FILE`)
- `_load_local_model_files()` same parameter change
- `download_model_files()` same parameter change (used by `quarry install`)
- `OnnxEmbeddingBackend.__init__` calls `select_provider()` to get the
  session directly, instead of creating its own `InferenceSession`
- Remove the `ort.InferenceSession(model_path)` call — the session comes
  from the provider module
- Log the selected provider and model file at INFO

### `src/quarry/backends.py` — NO CHANGE

`get_embedding_backend` still returns `OnnxEmbeddingBackend()`. The
provider selection is internal to `OnnxEmbeddingBackend.__init__`.

### `src/quarry/types.py` — NO CHANGE

`EmbeddingBackend` protocol is unchanged.

### `src/quarry/doctor.py` — MODIFY

`download_model_files()` call in `quarry install` downloads the int8
model (current behavior). Add a second call for the FP16 model if CUDA
is available, so users on GPU machines pre-cache both models. This is
optional — first-use download is the fallback.

## 5. API Surface

### Public (importable)

```python
# provider.py
@dataclass(frozen=True)
class ProviderSelection:
    provider: str       # e.g. "CUDAExecutionProvider"
    model_file: str     # e.g. "onnx/model_fp16.onnx"
    session: ort.InferenceSession

def select_provider() -> ProviderSelection: ...
```

### Internal (unchanged public API)

```python
# embeddings.py — signature changes
def download_model_files(model_file: str = ONNX_MODEL_FILE_DEFAULT) -> tuple[str, str]: ...
def _load_model_files(model_file: str) -> tuple[str, str]: ...
def _load_local_model_files(model_file: str) -> tuple[str, str]: ...
```

`OnnxEmbeddingBackend` keeps its existing public interface. The
`EmbeddingBackend` protocol is unchanged. No caller sees the provider
selection.

## 6. Testing Strategy

All tests run on CPU-only CI. No GPU required.

### Unit tests for provider selection (`tests/test_provider.py`)

**Mock `ort.get_available_providers()`** to simulate different hardware:

| Test | Mock returns | Expected |
|------|-------------|----------|
| CUDA available + session succeeds | `["CUDAExecutionProvider", "CPUExecutionProvider"]` + session mock | CUDA + FP16 |
| CUDA available + session fails | `["CUDAExecutionProvider", "CPUExecutionProvider"]` + session raises | CPU + int8, WARNING logged |
| CPU only | `["CPUExecutionProvider"]` | CPU + int8 |
| `QUARRY_PROVIDER=cpu` with CUDA available | `["CUDAExecutionProvider", "CPUExecutionProvider"]` | CPU + int8 (override) |
| `QUARRY_PROVIDER=cpu` with CPU only | `["CPUExecutionProvider"]` | CPU + int8 |

**Mock `ort.InferenceSession`** to avoid loading real models. The mock
returns a session object that satisfies the `session.run()` interface.

**Use `monkeypatch.setenv` / `monkeypatch.delenv`** for `QUARRY_PROVIDER`.

**Assert log messages** using `caplog` fixture to verify INFO/WARNING
output matches the spec.

### Existing test compatibility

`tests/test_embeddings.py` already mocks `InferenceSession` and
`_load_model_files`. After the change, the mock target shifts:
- Mock `select_provider()` instead of `InferenceSession` directly
- Or mock at the same level (`onnxruntime.InferenceSession`) since
  `select_provider` calls it internally

The second approach (mock `InferenceSession`) requires no test changes.
`select_provider` will call `InferenceSession` with the mock, get the
mock session back, and pass it to `OnnxEmbeddingBackend`. Existing tests
continue to work.

### Integration test (manual, not CI)

On a GPU machine, run:
```bash
QUARRY_PROVIDER=cpu uv run python -c "from quarry.embeddings import OnnxEmbeddingBackend; b = OnnxEmbeddingBackend(); print(b.embed_texts(['test']).shape)"
```
Then without the override. Verify logs show the expected provider.

## 7. Migration

### Transparent. No user action required.

- **Existing CPU installs**: behavior is identical. `select_provider()`
  detects CPU only, selects int8. Same model, same session, same vectors.

- **New GPU installs**: `select_provider()` detects CUDA, downloads FP16
  model on first use (~218 MB one-time download). Subsequent startups
  use the cached model. Embeddings are compatible — same 768-dim FP32
  vectors.

- **Existing databases**: no re-ingestion needed. All model precisions
  produce the same 768-dim FP32 embedding vectors from the same
  checkpoint. The `np.asarray(..., dtype=np.float32)` cast normalizes
  output regardless of internal precision.

- **`quarry install`**: continues to download int8 model. On GPU
  machines, also downloads FP16 model (additive, not breaking).

- **Config files**: no new config fields. `QUARRY_PROVIDER` env var is
  opt-in; absence means auto-detect.

## 8. Performance Considerations

### Model download

| Model | Size | When |
|-------|------|------|
| int8 | ~120 MB | `quarry install` or first use (current behavior) |
| FP16 | ~218 MB | First use on GPU machine, or `quarry install` on GPU |

FP16 download happens once. `huggingface_hub` caches by
(repo, revision, filename). No redundant downloads.

### Session creation overhead

`ort.InferenceSession()` takes 0.5-2s depending on model size and
provider. This happens once per process (cached in `backends.py`). The
provider probe does not create a throwaway session — it creates the
production session and returns it.

### CUDA validation cost

Zero additional cost. The validation *is* the session creation. If CUDA
works, we keep the session. If it fails, we create a CPU session instead.
Worst case (CUDA fails): one failed session creation (~1-2s) plus one
successful CPU session creation (~0.5s). This happens once per process.

### Batch size

Unchanged at 32. GPU machines could benefit from larger batches, but
that's a separate optimization. The current batching already prevents
OOM on both CPU and GPU.

### Memory

FP16 model uses ~218 MB vs int8's ~120 MB. On GPU machines this loads
into GPU VRAM, not system RAM. On CPU-only machines, the int8 model is
used as before.

### Thread safety

`select_provider()` is called inside the double-checked lock in
`backends.py`'s `get_embedding_backend()`. No additional synchronization
needed. The `ProviderSelection` dataclass is frozen/immutable.
