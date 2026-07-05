# Hybrid search design

Quarry answers a query by running two retrieval channels in parallel and
fusing their results. The first channel is dense vector similarity: the query
is embedded with the same model used at ingest, and the nearest chunk vectors
are retrieved by cosine distance. The second channel is BM25 full-text search
over the raw chunk text, which captures exact term and identifier matches that
a dense model can blur.

## Why two channels

A dense embedding is strong on paraphrase and concept — it finds the chunk
about quantifiers even when the query says "for-all and there-exists". BM25 is
strong on rare tokens — a function name, an error code, a spelling that the
embedding averages away. Neither alone is sufficient for a developer knowledge
base, so quarry runs both and fuses them.

## Reciprocal rank fusion

The two ranked lists are combined with reciprocal rank fusion. Each result
contributes one over k plus its rank to a document's fused score, and the
documents are re-sorted by that score. RRF needs no score calibration between
channels because it consumes ranks, not raw distances, which is why it is
robust when one channel returns cosine distances and the other BM25 scores.

## Over-fetch and cut

Each channel fetches a multiple of the requested limit so fusion has enough
candidates to reorder, then the fused list is cut to the requested limit. The
multiplier trades recall for latency; the default over-fetches three times the
limit.
