# Improving Agent Memory on LanceDB

## The Problem

You have an agent that needs to remember a large body of seeded knowledge and accumulate new memories as it works. Your current setup uses LanceDB with custom ingestion and recall. This document covers what the highest-performing memory systems do differently and how to apply those techniques locally, without remote API calls.

## How Vector-Only Recall Falls Short

A typical vector-based memory system works like this:

1. Text is split into chunks
2. Each chunk is embedded (turned into a high-dimensional numeric vector)
3. At recall time, the query is embedded and compared to stored vectors using cosine similarity
4. The top-k most similar chunks are returned

This works well for semantic similarity — "find me things that are *about* roughly the same topic." But it has systematic blind spots:

- **Exact terms get lost.** Embeddings capture meaning, not spelling. A query for "LanceDB" might return chunks about "vector databases" or "embedded storage" but miss a chunk that mentions LanceDB by name in a different context. This is especially problematic for proper nouns, code identifiers, and domain jargon.
- **Temporal reasoning is absent.** Vectors encode *what*, not *when*. If you ask "what did the agent learn yesterday about the auth flow?", vector similarity has no way to weight recency or filter by time.
- **Relationships are invisible.** Vector search treats each chunk as independent. It doesn't know that Fact A and Fact B are about the same entity, or that Fact C supersedes Fact A.
- **Uniform treatment of memory types.** A hard fact ("the API rate limit is 100 req/s"), an opinion ("the codebase is well-structured"), and a procedural memory ("when deploying, always run migrations first") are all stored and retrieved identically, even though they serve different purposes and should be weighted differently.

The systems that score highest on memory benchmarks — Hindsight (91.4% on LongMemEval), Memori (81.95% on LOCOMO) — all address these gaps through two main strategies: **structured ingestion** and **multi-channel retrieval**.

## Strategy 1: Structured Ingestion

The difference between a high-performing memory system and a basic one is mostly determined at *write time*, not read time. What you do when a memory enters the system determines how well you can find it later.

### Chunk with overlap and context

When splitting your seed corpus into chunks, each chunk should carry enough context to be useful in isolation. A 512-token chunk ripped from the middle of a document often lacks the context to be interpreted correctly.

Recommended approach:

- Use overlapping chunks (e.g., 512 tokens with 128-token overlap)
- Prepend a brief context header to each chunk: the document title, section heading, and a one-line summary of the chunk's content
- Store the original document structure (which chunks belong to which document, in what order) so you can expand context at recall time if needed

### Extract structured metadata at ingestion

For each chunk, run an LLM pass (local model on your DGX Spark works here) to extract:

| Metadata Field | Purpose | Example |
|---|---|---|
| **Entities** | Named things mentioned | `["LanceDB", "Lance format", "IVF-PQ index"]` |
| **Topics/Tags** | High-level categories | `["storage", "indexing", "performance"]` |
| **Memory type** | What kind of knowledge this is | `fact`, `opinion`, `procedure`, `observation` |
| **Timestamp** | When this was created or learned | `2026-03-25T10:00:00Z` |
| **Source** | Where it came from | `seed:architecture-doc` or `agent:task-42` |
| **Summary** | One-sentence distillation | `"LanceDB uses the Lance columnar format..."` |

This metadata lives alongside the embedding in LanceDB. LanceDB stores Arrow-compatible columnar data, so adding these fields is straightforward — they're just additional columns in the table.

### Generate multiple embeddings per memory

The highest-scoring memory systems don't embed just the raw text. They embed multiple representations:

1. **Raw chunk embedding** — the full text, as you do now
2. **Summary embedding** — embed the one-sentence summary separately
3. **Question embedding** — have the LLM generate 2-3 questions this chunk could answer, then embed those

The question embedding is particularly powerful. At recall time, the user's query is often phrased as a question, and question-to-question similarity tends to outperform question-to-passage similarity.

You can store these as separate rows with a shared `chunk_id`, or as separate vector columns in the same row (LanceDB supports multiple vector columns).

### Tag memories by type

This idea comes from the Hindsight architecture, which separates memories into four networks:

- **Facts** — Objective, verifiable information. "The API rate limit is 100 req/s."
- **Observations** — Preference-neutral summaries of entities. "The auth service has three endpoints."
- **Opinions/Beliefs** — Subjective assessments with confidence. "The codebase is well-structured (confidence: 0.7)."
- **Procedures** — How-to knowledge. "When deploying, run migrations before restarting the service."

When the agent appends new memories during work, it should classify each one. At recall time, you can weight these differently depending on the query. A factual question should prefer facts; a "how do I..." question should prefer procedures.

## Strategy 2: Multi-Channel Retrieval

This is where the biggest gains come from. Instead of relying on a single vector similarity search, you run multiple retrieval channels in parallel and fuse the results.

### The channels

**Channel 1: Vector similarity (semantic search)**

What you already have. Embed the query, find the nearest vectors. This channel is good at finding *topically related* memories even when the wording is different.

**Channel 2: Full-text search (BM25/keyword)**

LanceDB has native full-text search support. BM25 is a term-frequency-based ranking algorithm — it finds documents that contain the query's exact words, weighted by how rare those words are in the corpus. This catches what vector search misses: exact names, identifiers, specific terms.

**Channel 3: Metadata filtering**

Use the structured metadata from ingestion to filter before or during retrieval:

- Time-based: "memories from the last hour" or "memories from the seed corpus"
- Entity-based: "memories mentioning the auth service"
- Type-based: "only facts" or "only procedures"
- Source-based: "only agent-generated memories" or "only from document X"

**Channel 4: Temporal weighting**

Apply an exponential decay function to score memories by recency. A memory accessed or created recently gets a boost. The decay rate is a tunable parameter — fast decay for rapidly-changing domains, slow decay for stable reference material.

A simple decay function:

```
temporal_score = exp(-decay_rate * hours_since_creation)
```

For seed corpus memories that should remain stable, you can set a flag that exempts them from decay, or use a very slow decay rate.

### Fusing the results: Reciprocal Rank Fusion (RRF)

Each channel returns a ranked list of memories. RRF combines them without needing to normalize scores across channels (which is hard because vector similarity scores and BM25 scores are on completely different scales).

The algorithm:

```
For each memory m that appears in any channel's results:
    RRF_score(m) = Σ (1 / (k + rank_in_channel))
```

Where `k` is a constant (typically 60) that controls how much top-ranked results dominate. You sum across all channels where the memory appears.

This is ~30 lines of Python to implement. The result is a single merged ranking that captures semantic similarity, keyword matches, metadata relevance, and recency in one pass.

### Practical implementation sketch

```python
def recall(query: str, top_k: int = 10, filters: dict = None) -> list:
    """Multi-channel retrieval with RRF fusion."""
    
    # Channel 1: Vector similarity
    query_embedding = embed(query)
    vector_results = table.search(query_embedding).limit(top_k * 2).to_list()
    
    # Channel 2: Full-text search
    fts_results = table.search(query, query_type="fts").limit(top_k * 2).to_list()
    
    # Channel 3: Metadata filtering (applied as pre-filter)
    if filters:
        vector_results = [r for r in vector_results if matches_filters(r, filters)]
        fts_results = [r for r in fts_results if matches_filters(r, filters)]
    
    # Channel 4: Temporal weighting (applied as score modifier)
    for r in vector_results + fts_results:
        r["temporal_score"] = math.exp(-DECAY_RATE * hours_since(r["timestamp"]))
    
    # RRF Fusion
    scores = defaultdict(float)
    k = 60
    
    for rank, r in enumerate(vector_results):
        scores[r["chunk_id"]] += (1 / (k + rank)) * r["temporal_score"]
    
    for rank, r in enumerate(fts_results):
        scores[r["chunk_id"]] += (1 / (k + rank)) * r["temporal_score"]
    
    # Sort by fused score, return top_k
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [lookup(chunk_id) for chunk_id, _ in ranked[:top_k]]
```

## Seeding Strategy

For the initial large corpus load, the ingestion pipeline runs once in batch:

1. **Parse documents** into logical sections (respect existing structure like headings, paragraphs)
2. **Chunk with overlap** and context headers
3. **LLM enrichment pass** — extract entities, topics, memory type, summary, and generate question embeddings. This is the expensive step. On your DGX Spark, you can run a local model (Llama 3 70B or similar) for this. Budget roughly 1-2 seconds per chunk.
4. **Embed** all text representations (raw, summary, questions)
5. **Bulk insert** into LanceDB

For a corpus of, say, 10,000 chunks, the LLM enrichment pass is the bottleneck. At 1 second per chunk, that's ~3 hours on a single model instance. Parallelism on the DGX Spark can cut this significantly.

Mark all seed memories with `source: "seed"` and consider exempting them from temporal decay.

## Agent-Generated Memories

When the agent creates new memories during work, the same pipeline runs but in a lighter mode:

- The chunk is typically small (a single observation or fact), so no splitting needed
- LLM enrichment is still worth doing but can use a smaller/faster model
- Mark with `source: "agent:task-{id}"` and the current timestamp
- These memories *do* get temporal decay applied

### Memory consolidation (optional, high-value)

Periodically (e.g., end of each session), run a consolidation pass:

1. Retrieve all agent-generated memories from the session
2. Ask the LLM to identify duplicates, contradictions, and superseded facts
3. Merge duplicates, flag contradictions, and mark old versions as superseded (don't delete — keep for audit)
4. Generate consolidated summaries of related memories

This mimics what Hindsight calls "memory evolution" and prevents the memory store from accumulating noise over time.

## What to Skip

- **Graph databases (Neo4j, etc.):** Overkill for local-only unless your domain is heavily relational (e.g., organizational hierarchies, dependency graphs). The entity extraction + metadata approach gives you 80% of the benefit at 20% of the complexity.
- **Letta's OS-paging model:** Adds latency and architectural complexity. You're seeding upfront, not managing scarce context windows.
- **Mem0/Zep as products:** Cloud-first, remote-call-dependent. The *ideas* from their architectures (temporal graphs, graph-enhanced retrieval) are worth borrowing, but the products don't fit your local-only constraint.
- **Embedding model fine-tuning:** Not worth it unless you have a very narrow domain and a labeled evaluation set. General-purpose embedding models (e.g., `nomic-embed-text`, `bge-large`, `gte-large`) work well enough. Focus effort on retrieval fusion instead.

## Evaluation

How to know if the changes are working:

1. **Build a test set.** Write 50-100 questions that your memory system should be able to answer from the seeded corpus. Include factual lookups, temporal queries ("what was the most recent X?"), and relational queries ("what entities are related to X?").
2. **Measure recall@k.** For each question, check whether the correct chunk appears in the top-k results. Compare your current system against the multi-channel version.
3. **Measure mean reciprocal rank (MRR).** Where in the ranking does the correct answer appear? MRR penalizes systems that return the right answer at position 8 instead of position 1.
4. **A/B test retrieval channels.** Run each channel alone, then the fused version. This tells you which channels contribute the most to your domain.

## Summary

The highest-impact changes, in priority order:

1. **Add full-text search** alongside vector search — LanceDB supports this natively. Minimal effort, immediate improvement on exact-term queries.
2. **Implement RRF fusion** to combine vector + FTS results. ~30 lines of code.
3. **Add structured metadata** at ingestion time (entities, topics, memory type, timestamps). Requires an LLM pass but dramatically improves filtering and relevance.
4. **Generate question embeddings** during ingestion for better query-to-memory matching.
5. **Add temporal weighting** for agent-generated memories so recent observations outrank stale ones.
6. **Implement memory consolidation** to prevent noise accumulation over long-running agents.
