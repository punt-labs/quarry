# Multi-stage build for quarry serve
# Stage 1: Install Python dependencies
# Stage 2: Download embedding model (cached across rebuilds)
# Stage 3: Slim runtime image

FROM python:3.13-slim AS deps

WORKDIR /app
RUN pip install --no-cache-dir uv==0.7.13
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev

# Download the embedding model at build time so cold starts are fast.
# The model is ~120MB (int8 quantized) and cached in the HuggingFace hub.
FROM deps AS model
RUN uv run python -c "from quarry.embeddings import download_model_files; download_model_files()"

FROM python:3.13-slim AS runtime

WORKDIR /app

# Copy installed packages and application
COPY --from=deps /app /app

# Copy cached HuggingFace model from the model stage
COPY --from=model /root/.cache/huggingface /root/.cache/huggingface

# Data directory for LanceDB (mounted as a persistent volume)
RUN mkdir -p /data
ENV QUARRY_ROOT=/data

EXPOSE 8080

# Run quarry serve on 0.0.0.0:8080
# QUARRY_API_KEY is set via fly secrets, not baked into the image.
CMD ["/app/.venv/bin/quarry", "serve", "--host", "0.0.0.0", "--port", "8080"]
