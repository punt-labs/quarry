# quarry sync OOM: embed_texts() has no batching, 66 GB resident memory on 24 GB machine

## Summary

`quarry sync --db prfaq` consumed **66.3 GB of resident memory** on a 24 GB M2 MacBook Air, causing macOS to kill 120+ system services via Jetsam (`vm-compressor-space-shortage`) and effectively crashing the machine. The user had to force restart.

## Environment

- **Machine**: Mac14,2 (M2 MacBook Air, 24 GB RAM)
- **OS**: macOS 15.7.3 (24G419)
- **Python**: 3.13
- **Command**: `uv run quarry sync --db prfaq` (default `--workers 4`)
- **Collection**: 53 documents registered (PDFs, XLSX, DOCX), ~65 files total in the directory

## Root Cause

`OnnxEmbeddingBackend.embed_texts()` (`embeddings.py:107-125`) processes **all chunks for a document in a single ONNX inference call** with no batching:

```python
def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
    encodings = self._tokenizer.encode_batch(texts)       # ALL at once
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    _token_embeddings, sentence_embedding = self._session.run(
        None,
        {"input_ids": input_ids, "attention_mask": attention_mask},  # ALL at once
    )
    return sentence_embedding
```

For a document like `Q1-2024-PitchBook-NVCA-Venture-Monitor-Summary-XLS.xlsx` (70 sheets, **575 chunks**), this creates a single ONNX forward pass with batch shape `(575, 512)`. The transformer attention matrices alone (batch x heads x seq x seq) require multi-GB allocations per layer.

Combined with `ThreadPoolExecutor(max_workers=4)` in `sync.py:163`, up to 4 documents are embedded concurrently. At the time of the crash, 4 large documents were being embedded simultaneously:

| Document | Chunks | Embedding started |
|---|---|---|
| PitchBook NVCA Summary XLSX | 575 | 09:59:18 |
| SSRN-id1942821.pdf (136 pages) | 236 | 10:28:35 |
| SSRN-id1983115.pdf (158 pages) | 270 | 10:37:11 |
| SSRN-id2053258.pdf (52 pages) | 106 | 10:38:34 |

Total: **1,187 chunks** being embedded concurrently with no batching. ONNX Runtime releases the GIL during inference, so all 4 threads truly run in parallel, each allocating its own workspace.

## Evidence from Jetsam Report

`/Library/Logs/DiagnosticReports/JetsamEvent-2026-02-14-110027.ips`:

```text
largestProcess: python3.13

python3.13  PID 12112  rpages=4,344,914  (66.3 GB resident)  CPU time: 4788s  State: active
```

System memory at crash:

- **Free**: 69 MB (4,426 pages)
- **Compressor**: 12.5 GB (maxed out)
- **Reason**: `vm-compressor-space-shortage` on 120+ killed processes

The Python process was **not killed by Jetsam** — it survived while the OS killed everything around it, because macOS doesn't easily jetsam foreground processes. The machine became unresponsive.

## Memory Math

For snowflake-arctic-embed-m-v1.5 (12 heads, 768 hidden dim, 512 max seq length):

**Per-batch attention matrix**: `batch x 12_heads x 512 x 512 x 4 bytes`

- Batch 575: ~7.2 GB per layer
- Batch 270: ~3.4 GB per layer
- Batch 236: ~3.0 GB per layer
- Batch 106: ~1.3 GB per layer

With 4 concurrent threads: **~15 GB just for attention in a single layer**, before counting intermediate activations, feed-forward buffers, and outputs. ONNX Runtime allocates per-thread workspace that is not shared.

## Suggested Fix

Add batching to `embed_texts()`:

```python
EMBED_BATCH_SIZE = 32  # or 64

def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
    if not texts:
        return np.empty((0, self._dimension), dtype=np.float32)

    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        encodings = self._tokenizer.encode_batch(batch)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array(
            [e.attention_mask for e in encodings], dtype=np.int64
        )
        _, sentence_embedding = self._session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        all_embeddings.append(sentence_embedding)

    return np.concatenate(all_embeddings, axis=0)
```

With batch size 32:

- Attention per layer: `32 x 12 x 512 x 512 x 4 = 402 MB`
- 4 concurrent threads: ~1.6 GB total — well within 24 GB

Additionally, consider reducing `--workers` default from 4 to 2 for CPU-only embedding, since CPU-bound ONNX inference doesn't benefit from more threads than physical performance cores, and each thread multiplies peak memory.

## Performance Note

The PitchBook XLSX (575 chunks) had been embedding for **50+ minutes** before the crash. With batching, each batch of 32 takes only a few seconds — the total wall time would be similar, but peak memory drops from ~7+ GB to ~400 MB per thread.

## Steps to Reproduce

1. Register a directory containing a mix of large PDFs and XLSX files (total 50+ documents, some producing 200-575 chunks)
2. Run `quarry sync --db <name>` with default settings
3. Observe memory growth as large documents hit the embedding phase concurrently
