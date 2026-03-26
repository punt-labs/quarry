# Advanced Configuration

All settings are environment variables read via pydantic-settings. See the main [README](../README.md) for common settings.

## Full Settings Reference

### Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `QUARRY_ROOT` | `~/.quarry/data` | Base directory for all databases. Does not relocate `LOG_PATH`. |
| `LANCEDB_PATH` | `~/.quarry/data/default/lancedb` | Vector database location (overrides `--db`) |
| `REGISTRY_PATH` | `~/.quarry/data/default/registry.db` | Directory sync SQLite database |
| `LOG_PATH` | `~/.quarry/data/quarry.log` | Log file (rotating, 5 MB, 3 backups) |

### Chunking

| Variable | Default | Description |
|----------|---------|-------------|
| `CHUNK_MAX_CHARS` | `1800` | Target max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Overlap between consecutive chunks |

### Embedding

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING_MODEL` | `Snowflake/snowflake-arctic-embed-m-v1.5` | Model identifier (display only — the ONNX model is fixed) |
| `EMBEDDING_DIMENSION` | `768` | Vector dimension (display only — fixed at 768 by the ONNX model) |
