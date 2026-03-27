"""Benchmark ONNX embedding providers on NVIDIA GPU.

Compares int8+CPU, int8+CUDA, FP32+CUDA, and FP16+CUDA configurations
for snowflake-arctic-embed-m-v1.5.

Requires: pip install onnxruntime-gpu

Usage:
    uv run python benchmarks/bench_embedding.py
"""

from __future__ import annotations

import resource
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ID = "Snowflake/snowflake-arctic-embed-m-v1.5"
REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
TOKENIZER_FILE = "tokenizer.json"
BATCH_SIZE = 32
NUM_CHUNKS = 500
CHUNK_CHARS = 500


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchConfig:
    name: str
    model_file: str
    providers: list[str]


@dataclass
class BenchResult:
    name: str
    session_time_s: float = 0.0
    warmup_time_s: float = 0.0
    total_time_s: float = 0.0
    texts_per_s: float = 0.0
    max_rss_mb: float = 0.0
    active_providers: list[str] = field(default_factory=list)
    batch_times: list[float] = field(default_factory=list)
    error: str | None = None


CONFIGS = [
    BenchConfig("int8 + CPU", "onnx/model_int8.onnx", ["CPUExecutionProvider"]),
    BenchConfig(
        "int8 + CUDA",
        "onnx/model_int8.onnx",
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ),
    BenchConfig(
        "FP32 + CUDA",
        "onnx/model.onnx",
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ),
    BenchConfig(
        "FP16 + CUDA",
        "onnx/model_fp16.onnx",
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ),
    BenchConfig("FP32 + CPU", "onnx/model.onnx", ["CPUExecutionProvider"]),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_rss_mb() -> float:
    """Return process-lifetime max RSS in MB.

    macOS ``ru_maxrss`` is in bytes; Linux is in kilobytes.
    """
    import platform

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def _make_chunks() -> list[str]:
    """Build ~500 realistic text chunks from pipeline.py source."""
    src = Path(__file__).resolve().parent.parent / "src" / "quarry" / "pipeline.py"
    text = src.read_text() if src.exists() else ""

    # If the source file is too short, pad with repeated content
    while len(text) < NUM_CHUNKS * CHUNK_CHARS:
        text += textwrap.dedent("""\
            Semantic search enables finding documents by meaning rather than
            exact keyword match. Vector embeddings capture the semantic content
            of text passages, allowing similarity comparisons in high-dimensional
            space. The embedding model processes tokenized input through transformer
            layers and produces a fixed-length vector representation. Batch
            processing amortizes the overhead of model inference across multiple
            texts, improving throughput at the cost of latency for individual
            items. Quantization reduces model size and can speed up inference
            on CPU by using lower-precision arithmetic, though this may slightly
            reduce embedding quality. CoreML execution on Apple Silicon can
            leverage the Neural Engine for accelerated matrix operations.
        """)

    chunks: list[str] = []
    for i in range(0, len(text), CHUNK_CHARS):
        chunk = text[i : i + CHUNK_CHARS]
        if len(chunk) > 50:  # skip tiny trailing fragments
            chunks.append(chunk)
        if len(chunks) >= NUM_CHUNKS:
            break

    return chunks


def _download_model(filename: str) -> str:
    """Download or resolve a cached model file."""
    from huggingface_hub import hf_hub_download

    return hf_hub_download(
        repo_id=REPO_ID,
        filename=filename,
        revision=REVISION,
    )


def _load_tokenizer() -> object:
    """Load and configure the tokenizer."""
    from tokenizers import Tokenizer

    path = _download_model(TOKENIZER_FILE)
    tok = Tokenizer.from_file(path)
    tok.enable_padding()
    tok.enable_truncation(max_length=512)
    return tok


def _run_inference(
    session: object,
    tokenizer: object,
    batch: list[str],
) -> np.ndarray:
    """Run a single batch through the ONNX session."""
    encodings = tokenizer.encode_batch(batch)  # type: ignore[union-attr]
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    _token_emb, sentence_emb = session.run(  # type: ignore[union-attr]
        None,
        {"input_ids": input_ids, "attention_mask": attention_mask},
    )
    return np.asarray(sentence_emb, dtype=np.float32)


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(
    cfg: BenchConfig, tokenizer: object, chunks: list[str]
) -> BenchResult:
    """Run a single benchmark configuration."""
    import onnxruntime as ort

    result = BenchResult(name=cfg.name)
    print(f"\n{'=' * 60}")
    print(f"  {cfg.name}")
    print(f"  Model: {cfg.model_file}")
    print(f"  Requested providers: {cfg.providers}")
    print(f"{'=' * 60}")

    # --- Download model ---
    print("  Resolving model file...", flush=True)
    try:
        model_path = _download_model(cfg.model_file)
    except Exception as exc:
        result.error = f"Model download failed: {exc}"
        print(f"  ERROR: {result.error}")
        return result

    # --- Session creation ---
    print("  Creating InferenceSession...", flush=True)
    rss_before = _get_rss_mb()
    t0 = time.perf_counter()
    try:
        session = ort.InferenceSession(model_path, providers=cfg.providers)
    except Exception as exc:
        result.error = f"Session creation failed: {exc}"
        print(f"  ERROR: {result.error}")
        return result

    result.session_time_s = time.perf_counter() - t0
    rss_after_session = _get_rss_mb()
    result.active_providers = session.get_providers()
    print(f"  Session created in {result.session_time_s:.2f}s")
    print(f"  Active providers: {result.active_providers}")
    print(
        f"  RSS: {rss_before:.0f} MB -> {rss_after_session:.0f} MB"
        f" (+{rss_after_session - rss_before:.0f} MB)"
    )

    # --- Warmup ---
    print("  Running warmup batch...", flush=True)
    warmup_batch = chunks[:BATCH_SIZE]
    t0 = time.perf_counter()
    _run_inference(session, tokenizer, warmup_batch)
    result.warmup_time_s = time.perf_counter() - t0
    print(f"  Warmup: {result.warmup_time_s:.3f}s")

    # --- Throughput ---
    n_texts = len(chunks)
    n_batches = (n_texts + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"  Embedding {n_texts} texts in {n_batches} batches...", flush=True)

    t_total_start = time.perf_counter()
    for i in range(n_batches):
        batch = chunks[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
        t_batch = time.perf_counter()
        _run_inference(session, tokenizer, batch)
        elapsed = time.perf_counter() - t_batch
        result.batch_times.append(elapsed)
        if (i + 1) % 2 == 0 or i == n_batches - 1:
            print(
                f"    batch {i + 1}/{n_batches}: {elapsed:.3f}s ({len(batch)} texts)",
                flush=True,
            )

    result.total_time_s = time.perf_counter() - t_total_start
    result.texts_per_s = (
        n_texts / result.total_time_s if result.total_time_s > 0 else 0.0
    )
    result.max_rss_mb = _get_rss_mb()

    print(f"  Total: {result.total_time_s:.2f}s, {result.texts_per_s:.1f} texts/s")
    print(f"  Max RSS: {result.max_rss_mb:.0f} MB")

    # Clean up session to free memory before next config
    del session

    return result


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def print_summary(results: list[BenchResult]) -> None:
    """Print the final comparison table."""
    print(f"\n\n{'=' * 110}")
    print("  SUMMARY")
    print(f"{'=' * 110}")

    header = (
        f"{'Configuration':<23} | {'Session (s)':>11} | {'Warmup (s)':>10} "
        f"| {'Throughput (texts/s)':>20} | {'Max RSS (MB)':>13} | Providers"
    )
    sep = f"{'-' * 23}-+-{'-' * 11}-+-{'-' * 10}-+-{'-' * 20}-+-{'-' * 13}-+-{'-' * 30}"
    print(header)
    print(sep)

    for r in results:
        if r.error:
            print(f"{r.name:<23} | {'ERROR':>11} | {'':>10} | {r.error}")
        else:
            print(
                f"{r.name:<23} | {r.session_time_s:>11.2f} | {r.warmup_time_s:>10.2f} "
                f"| {r.texts_per_s:>20.1f} | {r.max_rss_mb:>13.0f}"
                f" | {r.active_providers}"
            )

    # Batch timing details
    print("\nBatch timing (seconds):")
    for r in results:
        if r.batch_times:
            avg = sum(r.batch_times) / len(r.batch_times)
            mn = min(r.batch_times)
            mx = max(r.batch_times)
            print(f"  {r.name:<23}: avg={avg:.3f}  min={mn:.3f}  max={mx:.3f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_gpu_info() -> None:
    """Print NVIDIA GPU info if available."""
    try:
        import subprocess

        result = subprocess.run(
            [  # noqa: S607
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            print(f"  GPU: {result.stdout.strip()}")
        else:
            print("  GPU: nvidia-smi failed")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  GPU: nvidia-smi not found")


def main() -> None:
    print("Preparing benchmark...")
    _print_gpu_info()
    print(f"  Batch size: {BATCH_SIZE}")
    print(f"  Target chunks: {NUM_CHUNKS}")

    chunks = _make_chunks()
    print(f"  Actual chunks: {len(chunks)}")
    print(f"  Avg chunk length: {sum(len(c) for c in chunks) / len(chunks):.0f} chars")

    tokenizer = _load_tokenizer()
    print("  Tokenizer loaded")

    results: list[BenchResult] = []
    for cfg in CONFIGS:
        result = run_benchmark(cfg, tokenizer, chunks)
        results.append(result)

    print_summary(results)


if __name__ == "__main__":
    main()
