# syntax=docker/dockerfile:1
#
# Tier-1 clean-machine harness for install.sh — mirrors ethos's clean-machine
# spike (../ethos/.tmp/clean-machine): a fresh, UNPRIVILEGED user with a truly
# clean $HOME, running the WORKING-TREE install.sh against a WORKING-TREE wheel
# (QUARRY_LOCAL_WHEEL), not the released curl|sh. This proves what the mock unit
# tests (tests/test_install_scripts.py) and the venv wheel test
# (scripts/test-wheel.sh) cannot: install.sh installs a working quarry from
# scratch on a machine that has never seen it.
#
# Base note: ethos ships a standalone Go binary, so its harness uses
# debian:bookworm-slim. quarry's runtime is a Python + ONNX stack, so this uses
# python:3.13-slim-bookworm — the same slim Debian userland plus the CPython and
# native runtime libraries onnxruntime / opencv-python-headless load. It is
# still a clean machine: no uv, no quarry, no prior state — install.sh bootstraps
# the entire toolchain (uv, the CLI, the model, TLS).
#
# Pinned to a manifest-list digest so amd64 (CI) and arm64 (local) resolve the
# identical base — a harness is only as good as its reproducible floor.

# python:3.13-slim-bookworm — manifest-list digest, resolves per-arch.
ARG BASE_DIGEST=sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

# ---- Stage 1: seed the embedding-model cache --------------------------------
# Downloads ONLY the two files quarry loads at runtime (INT8 ONNX + tokenizer),
# pinned to the exact repo + revision in src/quarry/config.py:
#   ONNX_MODEL_REPO     = "Snowflake/snowflake-arctic-embed-m-v1.5"
#   ONNX_MODEL_REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
# A single lightweight huggingface-hub fetch kept in its own layer, so a rebuild
# never re-downloads the ~120 MB model — the one slow step, paid once at build.
FROM python@${BASE_DIGEST} AS modelcache
ENV HF_HOME=/opt/hf-cache
# Pinned to quarry's resolved huggingface-hub (uv.lock) so a breaking hub release
# cannot make the harness flaky while the repo is unchanged — a digest-pinned
# base image with an unpinned tool is a false floor.
RUN pip install --no-cache-dir "huggingface-hub==1.30.0" \
 && python - <<'PY'
from huggingface_hub import hf_hub_download

REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
REV = "e58a8f756156a1293d763f17e3aae643474e9b8a"
for fname in ("onnx/model_int8.onnx", "tokenizer.json"):
    print("seeded", hf_hub_download(repo_id=REPO, filename=fname, revision=REV))
PY

# ---- Stage 2: the clean machine ---------------------------------------------
FROM python@${BASE_DIGEST} AS clean

# curl: install.sh bootstraps uv via `curl -LsSf https://astral.sh/uv/install.sh | sh`.
# git + ca-certificates: HTTPS to PyPI/HuggingFace/astral.sh and the plugin
# clone path's prerequisite check. No uv, no quarry — the installer builds them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Fresh, unprivileged user → a truly clean $HOME (no root, no prior quarry state).
RUN useradd -m tester
USER tester
WORKDIR /home/tester
ENV PATH=/home/tester/.local/bin:${PATH}

# Pre-seeded model cache in the user's OWN cache dir (owned + writable by tester),
# so install.sh's model download is a hit. HF_HOME defaults to ~/.cache/huggingface,
# whose hub/ subdir is exactly what the seed stage populated under /opt/hf-cache/hub.
COPY --from=modelcache --chown=tester:tester /opt/hf-cache /home/tester/.cache/huggingface

# The installer under test, the working-tree wheel, and the executable guide —
# copied last so a change to any of them does not bust the apt/model layers. The
# wheel is staged by tests/harness/build-and-run.sh (uv build) before the image
# builds; install.sh installs it via QUARRY_LOCAL_WHEEL, so THIS branch's package
# is what runs — not the released curl|sh version.
COPY --chown=tester:tester install.sh /home/tester/install.sh
# The wheel keeps its real PEP 427 name (uv rejects a renamed wheel); guide.sh
# globs $HOME/wheel/*.whl. `uv build --wheel` puts exactly one wheel in .stage/.
COPY --chown=tester:tester tests/harness/.stage/ /home/tester/wheel/
COPY --chown=tester:tester tests/harness/guide.sh /home/tester/guide.sh
RUN chmod +x /home/tester/guide.sh /home/tester/install.sh

CMD ["./guide.sh"]
