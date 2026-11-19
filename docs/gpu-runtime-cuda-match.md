# Design: match the onnxruntime-gpu build to the host CUDA major

**Bead:** quarry-ubj1
**Mission:** m-2026-07-26-001 (design stage)
**Status:** RATIFIED (operator ruled on O-1 2026-07-26 — see §7)
**Owner:** kpz
**Write-set produced by this design:** `src/quarry/gpu_runtime.py`,
`src/quarry/gpu_status.py`, `src/quarry/doctor.py`,
`tests/test_gpu_runtime.py` (implementation stage — not this doc's scope).
`gpu_status.py` + `doctor.py` are in the set because the operator chose to add
a distinct `CUDA_UNSUPPORTED` status (O-1).

---

## 1. The bug, stated plainly

`quarry install` swaps the CPU-only `onnxruntime` wheel for `onnxruntime-gpu`
when an NVIDIA GPU is present. Today it installs *whatever the resolver picks as
newest* under `onnxruntime-gpu>=1.18.0`. As of 2026-07, that is
`onnxruntime-gpu==1.27.0`, which links against `libcudart.so.13` (CUDA 13).

On a host whose system CUDA is 12 — driver 580, `libcudart.so.12` on the loader
path, **no** `libcudart.so.13` anywhere loadable — importing the freshly-installed
`onnxruntime` raises `ImportError` on the missing `libcudart.so.13`. onnxruntime
does not fall back to CPU when its own native `.so` fails to load: the *whole
module* fails to import. So the install leaves the daemon with an `onnxruntime`
that cannot be imported at all — strictly worse than the CPU wheel it replaced.

The observed contrast on the failing host (okinos, RTX 5080):

| wheel                    | libcudart it needs | import on a CUDA-12 host |
|--------------------------|--------------------|--------------------------|
| `onnxruntime-gpu==1.26.0`| `libcudart.so.12`  | works, CUDA provider up  |
| `onnxruntime-gpu==1.27.0`| `libcudart.so.13`  | `ImportError` — dead      |

A manual `uv pip install onnxruntime-gpu==1.26.0` fixes it, but the next
`quarry install` re-runs the swap and reverts to 1.27.0. The fix has to live
in the swap logic, not in a pin the user maintains by hand.

### 1.1 Root cause, cited by function and line

`src/quarry/gpu_runtime.py`:

- **Line 23** — the version constraint that lets the resolver pick a
  CUDA-mismatched wheel:

  ```python
  _ORT_GPU_SPEC = "onnxruntime-gpu>=1.18.0"
  ```

  A lower bound with no upper bound and no CUDA-major awareness. `uv pip install`
  resolves this to the newest published `onnxruntime-gpu`, independent of the
  host's CUDA runtime.

- **Lines 104–120** — `GpuRuntime._swap()` installs that spec verbatim:

  ```python
  gpu_install = self._pip("install", _ORT_GPU_SPEC)
  if gpu_install.returncode == 0:
      logger.info("onnxruntime-gpu installed successfully")
      self._clear_module_cache()
      return GpuStatus.INSTALLED
  ```

  The install returning `rc == 0` is treated as success. But `pip install`
  succeeding only means the *wheel downloaded and unpacked*. It says nothing
  about whether the resulting `onnxruntime` can `import` on this host. The CUDA
  mismatch is an *import-time* failure of a wheel that installed cleanly, so it
  slips straight past this success check into `GpuStatus.INSTALLED`.

- **Lines 52–59** — `GpuRuntime._resolve()` decides *whether* to swap
  (`_gpu_present()` and `_cuda_available()`) but never decides *which* wheel.
  There is no CUDA-major detection anywhere in the module. That is the missing
  step this design adds.

So there are two defects, and the fix must close both:

1. **Selection** — nothing maps the host CUDA major to a compatible
   `onnxruntime-gpu` version. (The primary bug.)
2. **Verification** — a clean `pip install` is accepted as success without
   proving the installed `onnxruntime` actually imports. (The reason the bug
   is invisible: it degrades to a broken import instead of failing loud.)

---

## 2. Background: how onnxruntime-gpu binds to CUDA

`onnxruntime-gpu` is a prebuilt wheel with native `.so` files compiled against a
*specific* CUDA major. It dynamically links `libcudart.so.<major>` (plus
cuBLAS, cuDNN). At import time the dynamic loader resolves those `SONAME`s
against the loader path. If the exact major is absent, `import onnxruntime`
raises — there is no minor-version or major-version fallback in CUDA's SONAME
scheme; `libcudart.so.12` and `libcudart.so.13` are different, non-interchangeable
libraries.

The empirically-anchored mapping (confirmed by the failing host: 1.26.0 imports,
1.27.0 does not):

| onnxruntime-gpu range | CUDA major | libcudart SONAME |
|-----------------------|-----------|-------------------|
| `>= 1.19, <= 1.26`    | 12        | `libcudart.so.12` |
| `>= 1.27`             | 13        | `libcudart.so.13` |

This table is the one piece of external knowledge the fix depends on. It is a
*published compatibility fact*, not an inference, and it belongs in the code as a
small, explicit, auditable data structure — not baked into a version string.

> Note on precision: the 1.27 boundary is the one that matters for the reported
> bug and is directly confirmed by the host. The lower edge (`>= 1.19`) reflects
> the CUDA-12 line onnxruntime has shipped since 1.19; the implementer verifies
> the exact floor against the PyPI release history at implementation time and
> records it inline. The *structure* — a mapping from CUDA major to a version
> range — is what this design fixes; the exact numbers are data the implementer
> pins with a citation.

---

## 3. Chosen approach

**Recommendation: (a) detect the host CUDA major and select a matching
`onnxruntime-gpu` version range.** Reject (b) — bundling `nvidia-*-cu<major>`
runtime wheels — for this codebase and this bug.

### 3.1 Why (a)

The problem is a *selection* problem: pick the wheel that matches the CUDA the
host already has and already works with. Detection + a version cap is the
smallest change that solves exactly that. The failing host proves the host's
CUDA 12 is fine — `onnxruntime-gpu==1.26.0` runs on it today. We do not need to
*provide* CUDA; we need to *stop installing the wheel that wants a CUDA the host
doesn't have*. That is one probe and one version bound.

This is the "I cannot simplify this any further" answer: the host has a working
CUDA 12; install the onnxruntime-gpu that targets CUDA 12. No new runtime
dependencies, no second copy of CUDA on disk, no divergence from the CUDA the
NVIDIA driver validated against.

### 3.2 Why not (b) — self-contained via `nvidia-*-cu<major>` wheels

Bundling the pip-provided CUDA runtime wheels (`nvidia-cuda-runtime-cu12`,
`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, …) so onnxruntime ignores system CUDA
is a real strategy — it is how PyTorch ships. But for quarry it is the wrong
trade:

1. **It does not remove the selection decision, it moves it.** You still must
   pick `-cu12` vs `-cu13` wheels, and that choice must still match the driver.
   Installing `-cu13` runtime wheels on a driver too old for CUDA 13 produces the
   same class of failure at a different layer (driver/runtime mismatch instead of
   missing SONAME). So (b) does not avoid CUDA-major detection — it *adds* a
   payload on top of it.
2. **Cost.** The CUDA runtime + cuDNN wheels are ~2–3 GB. Quarry embeds with one
   small ONNX model; paying multiple gigabytes to sidestep a one-line version cap
   is disproportionate.
3. **Two CUDAs on one host.** The bead notes CUDA 13 already exists on the host
   *bundled inside ollama*. Bundling a third CUDA copy for quarry multiplies the
   "which libcudart wins on the loader path" ambiguity that caused this bug.
4. **Driver ceiling still applies.** `nvidia-*-cu13` wheels still need a driver
   new enough for CUDA 13. Detection of the host's real capability is unavoidable,
   so we do the cheap version of it (probe what's loadable) rather than the
   expensive version (ship a runtime and hope the driver is new enough).

(b) is the right call for a project that must run on hosts with *no* system CUDA
at all. Quarry's GPU path only triggers when `nvidia-smi` already reports a
usable GPU (`_gpu_present()`), i.e. a driver and CUDA runtime are already
present. Detecting and matching that existing runtime is both sufficient and
minimal.

---

## 4. The design

### 4.1 Detection: what "host CUDA major" means and how to read it

The signal we need is: *which `libcudart.so.<major>` can the dynamic loader
resolve on this host right now?* That is precisely the thing onnxruntime's `.so`
will try to load at import, so it is the correct thing to match against — not the
driver's "maximum supported CUDA", which is a ceiling, not what is installed.

**Primary probe — `ldconfig -p`.** `ldconfig -p` lists every SONAME in the
loader cache. On the failing host it prints exactly:

```text
libcudart.so.12 (libc6,x86-64) => /lib/x86_64-linux-gnu/libcudart.so.12
```

and no `.so.13`. Parse the majors out of every `libcudart.so.N` line; the set of
resolvable CUDA runtime majors is the ground truth. This is what
`_detect_cuda_majors()` returns.

Properties that make this the right probe:

- It reads the *loadable* runtime, matching import-time reality. A driver that
  *supports* CUDA 13 but has no `libcudart.so.13` on the path correctly yields
  `{12}`, not `{13}` — which is exactly the okinos case.
- It is cheap (one subprocess, no GPU touched) and available wherever a CUDA
  runtime is installed.
- It composes with the existing subprocess style in the module (`shutil.which` +
  `subprocess.run`, the `# ruff: noqa: S603` trusted-binary rationale already at
  the top of the file).

**When `ldconfig` is absent or silent.** `ldconfig` is Linux/glibc-specific. The
GPU swap path only runs after `_gpu_present()` returned true, which means
`nvidia-smi` ran — a Linux-with-NVIDIA host, where `ldconfig` is present in
practice. If `ldconfig` is missing or lists no `libcudart`, detection yields the
empty set and we take the **fail-loud** branch (§4.4), not a guess.

Detection returns `frozenset[int]` (the CUDA majors resolvable on this host),
never `None`. Empty set is a meaningful, first-class result ("no CUDA runtime
found"), handled explicitly — not conflated with "detection failed".

### 4.2 Selection: map detected major → version spec

A single explicit table, the code form of §2:

```python
# CUDA major -> onnxruntime-gpu version spec that links that major's libcudart.
# Published compatibility facts, verified against PyPI at implementation time.
# 1.27.0 moved to CUDA 13 (libcudart.so.13); <=1.26 is the CUDA 12 line.
_ORT_GPU_BY_CUDA_MAJOR: Mapping[int, str] = {
    12: "onnxruntime-gpu>=1.19.0,<1.27.0",
    13: "onnxruntime-gpu>=1.27.0",
}
```

Selection rule, given the detected set of majors:

1. Take the **highest detected major that is a key in the table**. Highest,
   because if a host has both `libcudart.so.12` and `.so.13` loadable, the newer
   onnxruntime line is preferable and valid.
2. That yields the version spec to install.
3. If *no* detected major is a table key — empty detection, or only majors we
   have no mapping for (e.g. a future CUDA 14 with no onnxruntime line yet) —
   **do not install a GPU wheel.** Fail loud per §4.4.

This is a total function over the detected set: every input maps to either a
concrete spec or the explicit "unsupported" outcome. There is no silent default,
no "just install latest", no "assume 12".

### 4.3 Verification: prove the installed wheel imports

Closing the second defect (§1.1). After a successful `pip install`, before
declaring `INSTALLED`, run the *existing* provider-check subprocess
(`_cuda_available()`, lines 81–102) once more against the freshly-installed
package. It already spawns a clean subprocess specifically to avoid stale `.so`
state — the right tool. Interpretation:

- Subprocess imports onnxruntime **and** lists `CUDAExecutionProvider` → the swap
  worked. `GpuStatus.INSTALLED`.
- Subprocess **fails to import** (non-zero rc, e.g. the `libcudart.so.13`
  `ImportError`) → the wheel we selected does not run here. This must **not** be
  reported as success. Restore CPU (`_restore_cpu()`, the existing path) and
  return the recovered status.

This makes the success signal *"onnxruntime-gpu imports with CUDA"* rather than
*"pip exited 0"*. Even if the version table ever drifts from reality, the daemon
never ends up with an unimportable onnxruntime: verification catches it and the
CPU runtime is restored. That is the difference between "fails loud / recovers"
and "silently strictly worse than CPU".

### 4.4 Fail loud on genuinely unsupported hosts

Per PL-PP-3 and the standards' "fail loud on unsupported configs": when detection
yields no mappable CUDA major (empty set, or only unknown majors), the swap does
**not** proceed and does **not** silently mis-pin to a guessed version. It logs a
warning naming what was detected and what is supported, leaves the working CPU
`onnxruntime` in place, and returns a status the install display renders as a
warning (daemon still works on CPU), not a hard failure.

Rationale: on such a host there is no `onnxruntime-gpu` we can prove will import.
Installing a guess is exactly the bug we are fixing. Keeping CPU is the correct,
honest degradation — and it is *logged*, not silent, so `quarry doctor` surfaces
it. This is graceful degradation at the system boundary (GPU hardware/driver is a
boundary), which the standards permit; it is not a defensive fallback buried in
internal logic.

### 4.5 Data flow (end to end)

```text
GpuRuntime.ensure()
  └─ _resolve()
       ├─ _gpu_present()        no  → NO_GPU            (unchanged)
       ├─ _cuda_available()     yes → CUDA_PRESENT      (unchanged)
       └─ _swap()
            ├─ majors = _detect_cuda_majors()           NEW  (ldconfig probe)
            ├─ spec = _select_gpu_spec(majors)           NEW  (table lookup)
            │      └─ no mappable major → fail loud, keep CPU, warn  (§4.4)
            ├─ _pip("uninstall", "onnxruntime")          (unchanged)
            ├─ _pip("install", spec)                     (spec now CUDA-matched)
            ├─ rc != 0 → _restore_cpu()                  (unchanged)
            └─ _cuda_available()  (re-probe installed wheel)  NEW verification
                   ├─ imports + CUDA  → INSTALLED
                   └─ import fails     → _restore_cpu()   (recovered, warned)
```

Everything left of the `NEW` markers is unchanged. The module's public surface
(`GpuRuntime.ensure() -> GpuStatus`) and every existing `GpuStatus` value are
untouched, so `doctor.py:967-976` and the install display need no changes.

### 4.6 Exact function/logic changes

`src/quarry/gpu_runtime.py`:

1. **Delete** the constant `_ORT_GPU_SPEC = "onnxruntime-gpu>=1.18.0"` (line 23).
   No shim, no alias — it is gone (PL-PP-1).
2. **Add** `_ORT_GPU_BY_CUDA_MAJOR: Mapping[int, str]` — the §4.2 table, with an
   inline comment citing the CUDA-13 boundary and the PyPI-verification note.
3. **Add** a private method to probe CUDA majors:
   `def _detect_cuda_majors(self) -> frozenset[int]` — runs `ldconfig -p` via
   `shutil.which("ldconfig")` + `subprocess.run`, regex-extracts every
   `libcudart.so.(\d+)` major, returns the set. Empty set when `ldconfig` is
   absent or no `libcudart` line is found. (Method, not free function — it belongs
   to `GpuRuntime`, which owns the swap; PY-OO-7.)
4. **Add** a private method to select the spec:
   `def _select_gpu_spec(self, majors: frozenset[int]) -> str | None` — highest
   mappable major → its spec; `None` when nothing is mappable. The `str | None`
   is justified inline: `None` is the documented "no supported CUDA runtime"
   contract that drives the §4.4 fail-loud branch, not a give-up value
   (PY-EH-8 / PY-TS-14 — it is the "optional record / discriminated state" case).
5. **Rewrite** `_swap()` (lines 104–120) to: detect → select → (None ⇒ fail-loud
   warn + keep CPU) → uninstall → install selected spec → **verify import via
   `_cuda_available()`** → INSTALLED or restore-CPU. `_swap` stays a thin
   orchestrator that delegates to the new methods (PY-IC-6 single responsibility;
   keeps `_swap` complexity low).
6. `_cuda_available()`, `_restore_cpu()`, `_pip()`, `_clear_module_cache()`,
   `_gpu_present()`, `_resolve()`, `ensure()` — **unchanged**.

`src/quarry/gpu_status.py` and `src/quarry/doctor.py` (**O-1 resolved — operator
chose the distinct status, 2026-07-26**):

- **Add one `GpuStatus` member, `CUDA_UNSUPPORTED`**, for the §4.4 outcome: a GPU
  is present but no `onnxruntime-gpu` build can be proven to import (unmappable
  CUDA major, or no CUDA runtime resolvable at all), so the working CPU runtime
  is kept. Add its arm to the exhaustive `match` in `GpuStatus.outcome`
  (lines 38–50) with outcome `"recovered"` (the daemon works on CPU), and its
  symbol/label.
- **`doctor.py`** gets a specific branch so `quarry doctor` renders
  `CUDA_UNSUPPORTED` as *"GPU present but CUDA {major} unsupported — running on
  CPU"* (naming the detected vs supported majors), distinct from the generic
  restore message. This is the display refinement O-1 asked about.
- **Keep `GpuStatus.RESTORED`** for the §4.3 outcome that is genuinely a
  *rollback*: an `onnxruntime-gpu` wheel was selected and installed, then failed
  the post-install import re-probe, so CPU was reinstalled. Distinguishing the
  two ("we never had a valid GPU wheel to install" vs "we installed one and it
  didn't run") is exactly what the new status buys, and both share the
  `"recovered"` outcome so the install display still treats them as warnings,
  not hard failures.

### 4.7 OO / standards compliance for the touched file (the ratchet)

The mission requires a real OO improvement on the touched file, not
minimal ratchet-clearing. The concrete debt paydown this change funds:

- The new logic is **methods on `GpuRuntime`**, never module-level helpers next
  to the class (PY-OO-7). Detection, selection, and verification are behaviors of
  the thing that owns the swap.
- `_swap()` is decomposed from one procedure into orchestration + named steps
  (`_detect_cuda_majors`, `_select_gpu_spec`), lowering its cyclomatic
  complexity rather than growing it (PY-RF-3 Extract Method; keeps
  `max_complexity <= 10`).
- The CUDA-major mapping is a typed `Mapping[int, str]` constant with a rationale
  comment, not a magic string (PY-CS-1 constants, PY-TS-14 justified typing).
- `from __future__ import annotations` and full annotations already present;
  preserved. No new public attributes, no `__init__` (the class already uses
  `__new__`, PY-CC-1).

Implementer runs `uv run --extra dev oo-ratchet oo src/quarry/gpu_runtime.py` before and
after; `make check` (with the three merge-base ratchets) must pass and at least
one metric must improve.

---

## 5. Test plan

All tests extend `tests/test_gpu_runtime.py`, following its existing style:
patch `quarry.gpu_runtime.shutil.which` and `quarry.gpu_runtime.subprocess.run`
with side-effect functions, assert on the returned `GpuStatus` **and** on the
exact commands issued. No real onnxruntime install, no real GPU — the swap is
subprocess orchestration, so it is tested by asserting the subprocess argv, which
is honest here (these are not ML mocks; they are the real system-boundary calls).

The mock `run_side_effect` gains an `ldconfig` branch returning a chosen
`libcudart` listing, so each test declares the host's CUDA runtime.

### Case matrix

1. **CUDA-12 host selects the CUDA-12 wheel** (the reported bug, primary
   regression test).
   - `ldconfig -p` → `libcudart.so.12` only. `nvidia-smi` ok, provider-check
     first pass → CPU only (swap needed), post-install verify pass → CUDA.
   - Assert: the `uv pip install` argv contains `onnxruntime-gpu>=1.19.0,<1.27.0`
     (the CUDA-12 spec) and **does not** contain a bare `onnxruntime-gpu>=1.18.0`
     or `1.27`. Result `INSTALLED`.
   - This test fails against today's code (which installs `>=1.18.0`, resolving to
     1.27.0). It is the failing-test-first for the fix.

2. **CUDA-13 host selects the CUDA-13 wheel.**
   - `ldconfig -p` → `libcudart.so.13`. Assert install argv contains
     `onnxruntime-gpu>=1.27.0`. Result `INSTALLED`.

3. **Both 12 and 13 loadable → picks 13 (highest mappable).**
   - `ldconfig -p` lists both `.so.12` and `.so.13`. Assert install argv is the
     `>=1.27.0` spec (newest valid line), not the 12 spec.

4. **No GPU / no libcudart present → no GPU wheel installed.**
   - Two sub-cases:
     - `_gpu_present()` false (`nvidia-smi` absent): result `NO_GPU`, **zero** pip
       commands (already covered by `test_no_nvidia_smi`; keep, it now also
       guards that detection is not reached).
     - GPU present but `ldconfig` lists no `libcudart` at all (CUDA runtime
       missing): fail-loud branch — no `onnxruntime-gpu` install command issued,
       CPU `onnxruntime` left in place, result `CUDA_UNSUPPORTED`
       (recovered/warning), and a warning is logged. Assert the install argv list
       contains **no** `onnxruntime-gpu` entry.

5. **Unknown / newer-than-supported CUDA major → fail loud, do not mis-pin.**
   - `ldconfig -p` → `libcudart.so.14` only (a major with no table entry).
   - Assert: **no** `onnxruntime-gpu` install command is issued (crucially, it
     does **not** silently fall back to the 12 or 13 spec), CPU runtime retained,
     result `CUDA_UNSUPPORTED`, warning logged naming detected `{14}`
     and supported `{12, 13}`. Also assert `quarry doctor` renders the
     CUDA-major-specific message for this status.
   - This is the "don't silently mis-pin" guarantee, tested directly.

6. **Selected wheel installs but fails to import (verification catches it).**
   - `ldconfig` → `.so.12`, install rc 0, but the **post-install** provider-check
     subprocess returns non-zero (simulating the `libcudart` `ImportError`).
   - Assert: `_restore_cpu()` ran (CPU spec install issued), result `RESTORED`,
     **not** `INSTALLED`. Proves a clean `pip install` is no longer mistaken for
     success.

7. **`ldconfig` absent but GPU present.**
   - `shutil.which("ldconfig")` → `None`. Same outcome as case 4b: fail loud, keep
     CPU, warn, result `CUDA_UNSUPPORTED`. Guards the boundary where the probe
     tool itself is missing.

8. **Regression: unchanged early-exit paths still hold.**
   - `test_no_uv_on_path`, `test_cuda_already_available`,
     `test_swap_failure_restores_cpu`, `test_swap_failure_restore_also_fails`,
     `test_swap_success_clears_module_cache` — updated only where they hard-code
     `onnxruntime-gpu>=1.18.0` (they must now expect a CUDA-matched spec and feed
     an `ldconfig` branch). Behavior asserted (status values, restore-on-failure)
     is unchanged.

### Coverage note

Per PL-TT-2/PL-TT-3 the count of tests rises and error paths dominate the new
cases (4b, 5, 6, 7 are all failure/degradation paths). The primary bug (case 1)
gets an explicit regression test that fails on `main` and passes on the fix —
TDD as the workflow requires for a bug fix.

---

## 6. Rejected alternatives

1. **Bump the pin to `onnxruntime-gpu>=1.18.0,<1.27.0` and stop.** Fixes okinos
   today by capping below the CUDA-13 line, but it is not a fix — it is a
   different hard-coded guess. It breaks the moment quarry runs on a genuine
   CUDA-13 host (which the driver-580 line will become), reintroducing the mirror
   image of this bug. Rejected: hard-coding a single CUDA major is the class of
   defect we are removing, not a fix for it.

2. **Parse `nvidia-smi`'s "CUDA Version:" field for the major.** That field
   reports the *maximum CUDA the driver supports*, a ceiling — not what is
   installed and loadable. On okinos it can read "13.x" while only
   `libcudart.so.12` exists on the path, so selecting by it would pick the 1.27
   wheel and reproduce the exact `ImportError`. The loadable-runtime probe
   (`ldconfig`) is the correct signal; the driver ceiling is the wrong one.
   Rejected as the primary signal (the driver ceiling could inform a future
   secondary check but is not needed here).

3. **Bundle `nvidia-*-cu<major>` runtime wheels (option (b)).** Analyzed in §3.2.
   Rejected for quarry: multi-GB payload for a small-model embedder, still
   requires the same CUDA-major decision, still bound by the driver ceiling, and
   adds a third CUDA copy to a host that already has two. Right for no-system-CUDA
   hosts, wrong for quarry's GPU path which only runs when a usable NVIDIA runtime
   is already present.

4. **Try `import onnxruntime` after installing latest, and on `ImportError`
   downgrade one version at a time until it imports.** A retry loop over versions
   "works" but is slow (multiple multi-hundred-MB installs), non-deterministic in
   which version it lands on, and hides the real signal (the host's CUDA major)
   behind trial-and-error. The explicit table + probe reaches the right version in
   one install and documents *why*. Rejected: guess-and-check where a lookup
   suffices; violates "measure, don't guess".

5. **Add a `# type: ignore` / broaden `_swap` to swallow the import error and
   report INSTALLED anyway.** Would satisfy `make check` and hide the bug — the
   precise anti-pattern the standards forbid (no suppression, fail loud). Rejected
   outright.

---

## 7. Open questions (operator input)

- **O-1 — distinct status for "unsupported CUDA major"? — RESOLVED (operator,
  2026-07-26): ADD `CUDA_UNSUPPORTED`.** The operator chose the distinct status
  over reusing `RESTORED`. Implementation adds a `GpuStatus.CUDA_UNSUPPORTED`
  member with `outcome "recovered"` and a `doctor.py` branch rendering *"GPU
  present but CUDA {major} unsupported — running on CPU"* (naming detected vs
  supported majors). `CUDA_UNSUPPORTED` is used for the §4.4 fail-loud branch (no
  mappable CUDA major / no CUDA runtime found); `RESTORED` remains the §4.3
  rollback outcome (a wheel was installed, then failed the import re-probe). See
  §4.6 for the exact changes.

- **O-2 — verify the exact CUDA-12 floor.** §4.2 uses `>=1.19.0` as the low edge
  of the CUDA-12 line. The 1.27 *upper* boundary is confirmed by the failing host
  and is the one that matters. The implementer verifies the exact 1.19 floor
  against onnxruntime's PyPI release history at implementation time and records
  the citation inline. Flagged so it is checked, not assumed. Not blocking — a
  slightly conservative floor (e.g. `>=1.20`) is still correct; only the ceiling
  is load-bearing.

---

## 8. Standards compliance summary

- **No migration/compat/shim code (PL-PP-1).** The old `_ORT_GPU_SPEC` constant is
  deleted, not aliased. No `>=1.18.0` fallback path is kept "just in case".
  Forward integration only.
- **No defensive fallbacks except at the system boundary (PL-PP-3, PY-EH-5).** The
  only degradation paths are (a) unsupported CUDA major and (b) selected wheel
  fails to import — both are the GPU-hardware/driver *boundary*, both are *logged*,
  and both leave a working CPU runtime. No try/except for flow control in internal
  logic; internal methods trust the invariants the probe establishes.
- **Fail loud on unsupported configs (PY-EH-8 spirit).** An unmappable CUDA major
  never silently mis-pins to a guessed version; it warns and keeps CPU. The
  `str | None` from `_select_gpu_spec` is the documented "no supported runtime"
  contract (a discriminated state, §4.6), justified inline per PY-TS-14 — not a
  give-up `None`.
- **Verification over optimism.** Success means "onnxruntime-gpu imports with
  CUDA", proven by the re-probe, not "pip exited 0".
- **OO ratchet.** New behavior lives as methods on `GpuRuntime`; `_swap`
  complexity drops via extraction; typed constant replaces a magic string. `make
  check` + three merge-base ratchets must pass with a metric improving on the
  touched file.
