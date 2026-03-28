# Review: Provider Auto-Detection Design (DES-016 Implementation)

Reviewer: kpz
Date: 2026-03-27

## Verdict

The design is sound. The probe algorithm, fallback chain, and model
mapping are correct. The "validate with a real session" approach is the
right call -- `get_available_providers()` is unreliable on every platform
I have seen. A few issues to fix before implementation.

## Issues

### 1. FP16 model download path is underspecified

The design says `_load_model_files()` gains a `model_file: str`
parameter, but does not address what happens when CUDA is selected and
the FP16 model is not cached.

Current flow:
1. `select_provider()` picks CUDA + FP16
2. It creates `InferenceSession("onnx/model_fp16.onnx")` -- but this
   needs an absolute filesystem path, not a relative HF repo path

The design conflates two things: the HF repo filename
(`"onnx/model_fp16.onnx"`) and the local filesystem path returned by
`hf_hub_download()`. `select_provider()` needs to resolve the HF
filename to a local path *before* creating the session. This means
`select_provider()` must call `_load_model_files(model_file)` to get the
absolute path, then pass that to `InferenceSession`.

**Fix**: Make the probe sequence explicit:
1. Determine candidate provider + model_file (the HF repo path)
2. Resolve model_file to a local path via `_load_model_files(model_file)`
3. Create `InferenceSession(local_path, providers=[candidate])`
4. If step 3 fails for CUDA, fall back to CPU + int8 (repeat steps 2-3)

### 2. ProviderSelection holds a session -- this couples provider.py to embeddings.py

`ProviderSelection` stores an `ort.InferenceSession`. This means
`provider.py` must import onnxruntime and create sessions. But
`embeddings.py` also needs to load the tokenizer, and currently creates
the session alongside the tokenizer in `__init__`.

The design says `OnnxEmbeddingBackend.__init__` calls
`select_provider()` to get the session directly. This works, but now
`provider.py` must handle model file resolution (downloading from HF),
which is currently `embeddings.py`'s job. The dependency graph becomes:

```
provider.py -> embeddings._load_model_files() -> huggingface_hub
embeddings.py -> provider.select_provider() -> ort
```

Circular import risk. Both modules call into each other.

**Fix**: Two options, pick one:

A. **Keep session creation in embeddings.py**. `select_provider()` returns
   only `(provider_name, model_file)` -- no session. `OnnxEmbeddingBackend`
   resolves the model path and creates the session itself. This is simpler
   and avoids the circular dependency. `ProviderSelection` becomes:

   ```python
   @dataclass(frozen=True)
   class ProviderSelection:
       provider: str
       model_file: str
   ```

   ~50 lines instead of ~80. Provider module has zero dependency on
   embeddings or huggingface_hub.

B. **Move `_load_model_files` to provider.py**. This consolidates all
   model resolution in one place but makes `provider.py` do too many
   things (hardware probing + HF caching + session creation).

I recommend option A. The session is an implementation detail of
`OnnxEmbeddingBackend`, not of provider detection. Provider detection
answers "what provider and precision?" -- session creation is separate.

### 3. QUARRY_PROVIDER only supports "cpu" -- extend or document the constraint

The env var accepts only `"cpu"`. What about `QUARRY_PROVIDER=cuda` to
force CUDA without fallback (fail loudly if CUDA is broken)? This is
useful for debugging GPU setups -- you want to see the error, not silently
fall back to CPU.

**Fix**: Support three values:
- `cpu` -- force CPU, skip CUDA probe
- `cuda` -- force CUDA, raise on failure (no fallback)
- unset -- auto-detect with fallback (current behavior)

This adds ~5 lines to the probe function. Document in README.

### 4. Session options are missing

`InferenceSession(model_path)` uses default session options. For
production use, two options matter:

- `sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL`
  -- enables all graph optimizations (constant folding, node fusion).
  Default is `ORT_ENABLE_BASIC`. On CPU, this can improve throughput
  10-20% for transformer models.

- `sess_options.inter_op_num_threads` and `intra_op_num_threads` --
  defaults to using all cores, which is fine for single-user, but worth
  documenting.

**Fix**: Create session options explicitly in `OnnxEmbeddingBackend.__init__`
with `ORT_ENABLE_ALL`. Benchmark before/after on both CPU and CUDA to
verify the improvement. Do not guess -- measure.

### 5. Test strategy says "mock InferenceSession" for provider tests -- this is correct but incomplete

The test table covers the probe logic well. Missing cases:

- **QUARRY_PROVIDER set to an unknown value** (e.g., `"rocm"`). Should
  this raise ValueError, log a warning and auto-detect, or silently
  ignore? Spec does not say.

- **get_available_providers() returns empty list**. Unlikely but possible
  if onnxruntime is installed without any EP. The code would fall through
  to step 5 and try `CPUExecutionProvider`, which would also fail. Add a
  test and decide the error behavior.

- **FP16 model not cached, download fails** (network error during CUDA
  probe). The probe creates a session with the FP16 model path. If the
  model is not on disk, `InferenceSession` raises. This falls through to
  CPU fallback, which uses int8 -- correct behavior, but the WARNING
  message should distinguish "CUDA driver issue" from "FP16 model not
  downloaded." Add a test for this case.

### 6. download_model_files in doctor.py needs both models

The design says `quarry install` on GPU machines should also download the
FP16 model. But the conditional ("if CUDA is available") means calling
`get_available_providers()` during install. This is fine, but note that
`quarry install` runs without a GPU on many CI/build machines. The
install command should always download int8 and optionally download FP16.

The design says this. Just confirming it is correctly scoped.

### 7. Log message in step 1 is wrong

Step 1 says log: `"Provider override: QUARRY_PROVIDER=cpu, using
CPUExecutionProvider + int8"`. The message leaks the model filename into
the log. Better:

```
"Provider override: cpu (QUARRY_PROVIDER env var)"
```

The model file is a detail. The provider is the decision. Log the
provider selection and model file separately at DEBUG.

## What is correct

- **Probe-once-per-process** via the backends.py cache. No redundant
  hardware probing.
- **No CoreML**. The benchmark data (0.8 texts/s, 99 partitions) is
  conclusive. Excluding it avoids a trap.
- **Session reuse** (creating the production session during probe, not a
  throwaway). Zero additional overhead.
- **Frozen dataclass** for `ProviderSelection`. Immutable, thread-safe.
- **No new config fields**. Env var only. Users who need control get it;
  everyone else gets auto-detect.
- **Migration is transparent**. Same model checkpoint, same embedding
  dimension, same FP32 output normalization.
- **Thread safety** via the existing double-checked lock in backends.py.

## Summary of recommended changes

| # | Change | Effort |
|---|--------|--------|
| 1 | Separate model path resolution from session creation in probe | Small |
| 2 | Drop session from ProviderSelection, keep in embeddings.py | Small |
| 3 | Support `QUARRY_PROVIDER=cuda` for force-fail mode | Small |
| 4 | Add explicit session options with ORT_ENABLE_ALL + benchmark | Medium |
| 5 | Add 3 missing test cases | Small |
| 6 | (Already correct, no change needed) | -- |
| 7 | Fix log message format | Trivial |

Items 1 and 2 are the structural changes. The rest are additive.
