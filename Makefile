.PHONY: help test lint type check format build clean depot bench-cuda docs docs-clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

test: ## Run tests (excludes slow integration tests)
	uv run pytest

lint: ## Lint and format check
	uv run ruff check .
	uv run ruff format --check .

type: ## Type check with mypy and pyright
	uv run mypy src/ tests/
	uv run pyright src/ tests/

check: lint type test ## Run all quality gates

format: ## Auto-format code
	uv run ruff format .
	uv run ruff check --fix .

build: ## Build wheel and sdist
	rm -rf dist/
	uv build
	uvx twine check dist/*

clean: ## Remove build artifacts
	rm -rf dist/ .tmp/

TEX_DOCS := prfaq docs/architecture docs/claude-code-quarry

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
