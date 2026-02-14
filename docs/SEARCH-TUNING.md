# Search Quality and Tuning

## Chunking Parameters

Quarry splits documents into overlapping chunks before embedding. Two environment variables control this:

| Variable | Default | Effect |
|----------|---------|--------|
| `CHUNK_MAX_CHARS` | `1800` | Target max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Characters shared between consecutive chunks |

### When to Adjust

**Increase `CHUNK_MAX_CHARS`** (e.g. 3000-4000) when:
- Documents have long, self-contained sections (legal contracts, academic papers)
- Search queries are broad and you want more context per result
- You're ingesting source code with large functions

**Decrease `CHUNK_MAX_CHARS`** (e.g. 800-1200) when:
- Documents are dense and each paragraph covers a distinct topic
- Search queries are specific and you want precise matches
- You need to stay well within the embedding model's 512-token window

**Increase `CHUNK_OVERLAP_CHARS`** (e.g. 400) when:
- Important information spans paragraph boundaries
- You're seeing relevant results cut off mid-thought

The embedding model (snowflake-arctic-embed-m-v1.5) has a 512-token context window. Chunks exceeding this are truncated during embedding, so very large `CHUNK_MAX_CHARS` values reduce embedding quality for the tail of each chunk.

## OCR Backend Choice

| Scenario | Recommended | Why |
|----------|-------------|-----|
| General document search | `local` (default) | Good enough for semantic search; no cloud dependency |
| High-accuracy text extraction | `textract` | Better character accuracy, especially for degraded scans |
| Scanned handwriting | `textract` | AWS Textract handles handwriting better than RapidOCR |
| Offline or air-gapped | `local` | No network required |
| Cost-sensitive bulk ingestion | `local` | Free; Textract charges per page |

Set via `OCR_BACKEND=textract` (requires AWS credentials and S3 bucket).

For semantic search, OCR accuracy matters less than you might expect. The embedding model is robust to minor OCR errors -- a misspelled word rarely changes the semantic meaning enough to affect search ranking. Use local OCR unless you need the extracted text for purposes beyond search (e.g. exact quoting).

## Embedding Model

Quarry uses **snowflake-arctic-embed-m-v1.5** via ONNX Runtime:
- 768-dimensional vectors
- 512-token context window
- Asymmetric retrieval: queries are prefixed with a search instruction, documents are not
- Runs on CPU only (~50ms per chunk on Apple Silicon)

The model is fixed -- there is no configuration to swap it. This is intentional: changing the model invalidates all existing embeddings, requiring full re-ingestion.

## Tips for Better Search Results

1. **Use natural language queries.** The model is trained on question-passage pairs. "What were Q3 revenue figures?" works better than "Q3 revenue".

2. **Use collection filtering for scoped search.** If you have separate projects, ingest them into different collections and filter at search time: `quarry search "auth logic" -c backend`.

3. **Re-ingest after tuning.** Chunk parameters only affect new ingestions. After changing `CHUNK_MAX_CHARS`, run `quarry sync` to re-process registered directories.

4. **Check chunk boundaries.** Use `quarry search -n 1 "your query"` and examine the result. If the answer is split across chunks, increase overlap or chunk size.

5. **Use `--overwrite` when re-ingesting.** Without it, duplicate chunks accumulate. `quarry ingest-file report.pdf --overwrite` replaces existing data cleanly.
