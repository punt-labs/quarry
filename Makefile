.PHONY: help test test-slow lint lint-docs lint-slash-mcp lint-hook-mcp-matcher type check check-full check-oo audit-oo update-oo check-coupling update-coupling check-suppressions update-suppressions check-imports check-openapi openapi report format install build test-wheel test-install-clean clean depot bench-cuda docs docs-clean metrics coverage eval eval-baseline logs-errors logs-tail

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

test: ## Run tests (excludes slow integration tests)
	uv run --extra dev pytest

# The slow tier the fast CI job never runs (-m 'not slow'): live-server / real-TLS
# smokes that can hang a shared runner. Run here or in the wheel gate (quarry-5pg1).
test-slow: ## Run the slow tier (real-TLS/live-server smokes)
	uv run --extra dev pytest -m slow

# EVERY invocation of a dev-extra tool runs under `--extra dev`, and the flag is
# load-bearing rather than tidy.  ruff, pytest, mypy, pyright and import-linter
# all live in the dev extra; `uv run` syncs only the DEFAULT set, so none of them
# is in a fresh .venv/bin and uv silently falls back to whatever is on PATH.  On
# one machine that was a system Python's ruff 0.13.0 and pytest — versions nobody
# pinned — which is how a red gate reproduced for a reviewer and not for the
# author, on a file neither had touched.  Same failure the PYRIGHT_VERSION pin
# below already guards against: a gate is worthless if it reports on a toolchain
# the lock does not name.
#
# The `uv run python tools/...` lines are deliberately NOT flagged and are not an
# oversight: `python` always comes from the project venv uv creates, so there is
# no PATH to fall back to, and those scripts are stdlib-only or import quarry
# itself, which is a default dependency.  Adding the flag there would imply a
# risk that does not exist.
#
# When adding a target, the test is not "is this a gate" but "does this command
# name a binary from the dev extra".  If it does, it takes the flag.
lint: lint-docs lint-slash-mcp lint-hook-mcp-matcher ## Lint and format check
	uv run --extra dev ruff check .
	uv run --extra dev ruff format --check .

lint-docs: ## Lint markdown files (matches CI docs job)
	npx markdownlint-cli2 "**/*.md"

# Guards against the disconnected proxy namespace re-appearing in slash-command
# instructions. The native quarry MCP server exposes tools as `mcp__quarry__*`
# (and `mcp__quarry-dev__*` for the dev twin); the old `plugin_quarry` proxy
# names no longer resolve. See quarry-ydym.
lint-slash-mcp: ## Fail if slash commands name the disconnected plugin_quarry namespace
	@if grep -rE 'mcp__plugin_quarry(-dev)?_quarry__' plugin/commands/ >/dev/null 2>&1; then \
		echo "plugin/commands/ references disconnected plugin_quarry namespace; use mcp__quarry__* / mcp__quarry-dev__*" >&2; \
		grep -rnE 'mcp__plugin_quarry(-dev)?_quarry__' plugin/commands/ >&2; \
		exit 1; \
	fi

# Guards against the PostToolUse matcher in plugin/hooks/hooks.json losing
# coverage of the native mcp__quarry__ / mcp__quarry-dev__ namespace during
# a rename. A matcher that only admits the retired plugin_quarry proxy
# would silently skip suppress-output.sh for every native tool call. The
# guard parses hooks.json, compiles the matcher regex, and asserts it
# admits concrete sample tool names — a substring lint could not tell an
# augmented matcher from one that requires the retired prefix. See
# quarry-ydym.
lint-hook-mcp-matcher: ## Fail if hooks.json's PostToolUse matcher drops mcp__quarry(-dev)?__ coverage
	@uv run python tools/lint_hook_matcher.py

# Pin the node-pyright binary to the uv.lock-pinned wrapper version so `make
# type` catches exactly what CI catches. The pyright-python wrapper can reuse a
# stale locally-cached node-pyright that is older — and laxer — than CI's pinned
# one, so `make check` could PASS code that CI's pyright then REJECTS. That is
# this PR's own incident: a cached 1.1.408 hid three reportDeprecated errors that
# CI's 1.1.411 flagged.  Forcing the version keeps the local and CI checkers
# identical.
PYRIGHT_VERSION = $(shell uv run --extra dev python -c "import importlib.metadata as m; print(m.version('pyright'))")

type: ## Type check with mypy and pyright
	uv run --extra dev mypy src/ tests/
	PYRIGHT_PYTHON_FORCE_VERSION=$(PYRIGHT_VERSION) uv run --extra dev pyright src/ tests/

# Base-comparison flags injected by CI (e.g. --base-ref <merge-base> --require-base).
# Empty locally, where the tools default base to `git merge-base origin/main HEAD`.
OO_BASE ?=
COUPLING_BASE ?=
SUPPRESSION_BASE ?=

check: lint type test check-oo check-coupling check-suppressions check-imports check-openapi ## Run all quality gates

openapi: ## Regenerate docs/openapi.json from the daemon FastAPI app
	uv run python tools/generate_openapi.py

check-openapi: ## Fail if docs/openapi.json is stale vs the daemon app
	uv run python tools/generate_openapi.py --check

check-oo: ## OO ratchet — touched files must not regress vs the merge-base baseline
	uv run python tools/oo_score.py src/quarry/ --check $(OO_BASE)

# CI-only completeness guard: every scored file must be recorded in the committed
# baseline. Runs in CI on the post-update-oo state (code and baseline in sync);
# it cannot join the local `check` chain, which requires each commit to improve a
# metric so the improved value diverges from the baseline until update-oo runs.
# See .github/workflows/lint.yml.
audit-oo: ## CI completeness guard — every scored file must be in the baseline
	uv run python tools/oo_score.py src/quarry/ --audit-completeness

update-oo: ## Update OO baseline after improvements (stage .oo-baseline.json and .oo-audit.jsonl)
	uv run python tools/oo_score.py src/quarry/ --update $(OO_BASE)

check-coupling: ## Coupling ratchet — merge-base scoped, touched files must not regress
	uv run python tools/oo_coupling.py src/quarry/ --check $(COUPLING_BASE)

update-coupling: ## Update coupling baseline (stage .oo-coupling-baseline.json and .oo-coupling-audit.jsonl)
	uv run python tools/oo_coupling.py src/quarry/ --update $(COUPLING_BASE)

check-suppressions: ## Suppression ratchet — base-commit scoped, count must not increase
	uv run python tools/suppression_ratchet.py src/quarry/ --check $(SUPPRESSION_BASE)

# DES-031 I1 client/engine boundary. A static contract over the import graph:
# no client process (quarry.__main__/hooks/mcp_server) or client library
# (quarry.client/api) may import an engine package. A violating import fails
# here — not in review. Companion to the runtime engine-sabotage test.
check-imports: ## Import-linter — enforce the DES-031 client/engine package boundary
	uv run --extra dev lint-imports --config .importlinter

update-suppressions: ## Update suppression baseline after justified additions
	uv run python tools/suppression_ratchet.py src/quarry/ --update

report: ## Full diagnostics (OO score + all checks, no fail-fast)
	-uv run python tools/oo_score.py src/quarry/ --threshold
	-uv run --extra dev mypy src/ tests/
	-uv run --extra dev ruff format --check .
	-uv run --extra dev ruff check --preview --select PLR6301,PLR0913,UP035,UP040,UP007,N,I,SIM,C1901,S101 .
	-PYRIGHT_PYTHON_FORCE_VERSION=$(PYRIGHT_VERSION) uv run --extra dev pyright src/ tests/
	-uv run --extra dev lint-imports --config .importlinter
	-uv run --extra dev pytest
	@echo "Report complete."

check-full: check test-wheel ## Full quality gate including wheel test

format: ## Auto-format code
	uv run --extra dev ruff format .
	uv run --extra dev ruff check --fix .

install: build ## Build and install wheel locally for manual testing
	uv tool install --force dist/*.whl

build: ## Build wheel and sdist
	rm -rf dist/
	uv build
	uvx twine check dist/*

test-wheel: build ## Test the built wheel in an isolated venv on port 8422
	bash scripts/test-wheel.sh

test-install-clean: ## Clean-machine Docker gate: run install.sh from scratch, assert the CLI-only path
	bash tests/harness/build-and-run.sh

clean: ## Remove build artifacts
	rm -rf dist/ .tmp/

TEX_DOCS := prfaq docs/architecture docs/claude-code-quarry
# Z-spec docs need fuzz.sty and Oxford Z fonts (oxsz*.mf).
FUZZ_TEX := $(CURDIR)/docs/tex//
export TEXINPUTS := $(FUZZ_TEX):
export MFINPUTS := $(FUZZ_TEX):

docs: ## Build all LaTeX documents
	@set -e; \
	for doc in $(TEX_DOCS); do \
		dir=$$(dirname $$doc); \
		base=$$(basename $$doc); \
		echo "Building $$doc.pdf..."; \
		cd $$dir && pdflatex -interaction=nonstopmode -halt-on-error $$base.tex > /dev/null; \
		if [ "$$base" = "prfaq" ]; then biber $$base > /dev/null || exit 1; fi; \
		pdflatex -interaction=nonstopmode -halt-on-error $$base.tex > /dev/null; \
		pdflatex -interaction=nonstopmode -halt-on-error $$base.tex > /dev/null; \
		cd $(CURDIR); \
	done
	@$(MAKE) --no-print-directory docs-clean
	@echo "Done."

docs-clean: ## Remove LaTeX build artifacts
	@for doc in $(TEX_DOCS); do \
		rm -f $$doc.aux $$doc.log $$doc.out $$doc.toc $$doc.bbl $$doc.blg $$doc.bcf $$doc.run.xml; \
	done

DEPOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))../.depot

depot: build ## Build and copy wheel to local depot
	@mkdir -p $(DEPOT)
	@cp dist/*.whl $(DEPOT)/
	@echo "depot: $$(ls dist/*.whl | xargs -n1 basename) -> $(DEPOT)/"

bench-cuda: ## Benchmark embedding providers (requires NVIDIA GPU)
	uv sync
	uv pip uninstall onnxruntime
	uv pip install onnxruntime-gpu
	.venv/bin/python benchmarks/bench_embedding.py

metrics: ## ABC complexity analysis (magnitude >200 needs attention)
	@python3 -c "from pathlib import Path; import re, math; src = Path('src/quarry'); rows = []; [rows.append((len(t:=f.read_text().splitlines()), sum(1 for l in t if re.match(r'^\s*([\w.]+\s*=[^=]|[\w.]+\s*[+\-*/%&|^]=)', l)), sum(1 for l in t if re.search(r'\w+\(', l) and not re.match(r'^\s*(def |class |#|from |import )', l)), sum(1 for l in t if re.search(r'\b(if|elif|else|except|assert|and|or|not|in|is)\b', l) and not re.match(r'^\s*#', l)), f.name)) for f in sorted(src.glob('*.py'))]; rows.sort(key=lambda r: -math.sqrt(r[1]**2+r[2]**2+r[3]**2)); print(f\"{'Module':<30} {'Lines':>6} {'A':>5} {'B':>5} {'C':>5} {'|ABC|':>7}\"); print('-'*62); [print(f'{n:<30} {loc:>6} {a:>5} {b:>5} {c:>5} {math.sqrt(a**2+b**2+c**2):>7.1f}') for loc,a,b,c,n in rows]; print('-'*62); over=[n for loc,a,b,c,n in rows if math.sqrt(a**2+b**2+c**2)>200]; print(f'Modules over 200: {len(over)}' + (f' — {\", \".join(over)}' if over else ''))"

coverage: ## Test coverage with HTML report
	uv run --extra dev pytest --cov=quarry --cov-report=html --cov-report=term-missing
	@echo "HTML report: htmlcov/index.html"

# Daemon log location + how many recent matching lines to show. Overridable per
# PY-BS-4 so the target runs against a fixture dir in tests and any host layout.
LOG_DIR ?= $(HOME)/.punt-labs/quarry/logs
LOG_LINES ?= 40

# Standalone diagnostic (like `report`) — deliberately NOT in `make check`:
# runtime log noise is not a code-quality gate. Always exits 0.
logs-errors: ## Scan daemon logs for errors/failures (diagnostic, always exits 0)
	@LOG_DIR="$(LOG_DIR)" LOG_LINES="$(LOG_LINES)" bash scripts/logs-errors.sh

logs-tail: ## Tail the most recent lines of the daemon stderr log
	@LOG_DIR="$(LOG_DIR)" LOG_LINES="$(LOG_LINES)" bash scripts/logs-tail.sh

# Sync eval alongside dev so running the harness never uninstalls the toolchain.
eval: ## Phase-1 retrieval eval harness — per-bucket MRR/success + pollution
	uv sync --extra dev --extra eval
	uv run --extra eval python -m tools.eval

eval-baseline: ## Regenerate the committed Phase-1 baseline (run + qrels + JSON)
	uv sync --extra dev --extra eval
	uv run --extra eval python -m tools.eval \
		--emit-baseline tools/eval/baselines/baseline.json
