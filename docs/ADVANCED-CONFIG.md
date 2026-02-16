# Advanced Configuration

All settings are environment variables read via pydantic-settings. See the main [README](../README.md) for common settings.

## Full Settings Reference

### Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `QUARRY_ROOT` | `~/.quarry/data` | Base directory for all databases and logs |
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
| `EMBEDDING_BACKEND` | `onnx` | `onnx` (local) or `sagemaker` (AWS) |
| `EMBEDDING_MODEL` | `Snowflake/snowflake-arctic-embed-m-v1.5` | Model identifier (cache key) |
| `EMBEDDING_DIMENSION` | `768` | Vector dimension |
| `SAGEMAKER_ENDPOINT_NAME` | | SageMaker endpoint name (required when `EMBEDDING_BACKEND=sagemaker`) |

### OCR

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_BACKEND` | `local` | `local` (RapidOCR, offline) or `textract` (AWS) |

### AWS Textract

Only relevant when `OCR_BACKEND=textract`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | | AWS secret key |
| `AWS_DEFAULT_REGION` | `us-east-1` | Must match S3 bucket and SageMaker endpoint region |
| `S3_BUCKET` | | S3 bucket for Textract uploads |
| `TEXTRACT_POLL_INITIAL` | `5.0` | Initial polling interval (seconds) |
| `TEXTRACT_POLL_MAX` | `30.0` | Max polling interval (1.5x exponential backoff) |
| `TEXTRACT_MAX_WAIT` | `900` | Max wait for Textract job (seconds) |
| `TEXTRACT_MAX_IMAGE_BYTES` | `10485760` | Max image size for sync API (10 MB) |
