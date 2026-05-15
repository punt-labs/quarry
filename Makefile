.PHONY: help test lint lint-docs type check check-full check-oo update-oo check-coupling update-coupling check-suppressions update-suppressions report format install build test-wheel clean depot bench-cuda docs docs-clean metrics coverage

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

test: ## Run tests (excludes slow integration tests)
	uv run pytest

lint: lint-docs ## Lint and format check
	uv run ruff check .
	uv run ruff format --check .

lint-docs: ## Lint markdown files (matches CI docs job)
	npx markdownlint-cli2 CLAUDE.md "docs/**/*.md"

type: ## Type check with mypy and pyright
	uv run mypy src/ tests/
	uv run pyright src/ tests/

check: lint type test check-oo ## Run all quality gates

check-oo: ## OO ratchet — must improve over baseline, never regress
	uv run python tools/oo_score.py src/quarry/ --check

update-oo: ## Update OO baseline after improvements (stage .oo-baseline.json and .oo-audit.jsonl)
	uv run python tools/oo_score.py src/quarry/ --update

check-coupling: ## Coupling/cohesion analysis (informational, not in check chain)
	uv run python tools/oo_coupling.py src/quarry/ --check

update-coupling: ## Update coupling baseline after improvements
	uv run python tools/oo_coupling.py src/quarry/ --update

report: ## Full diagnostics (OO score + all checks, no fail-fast)
	-uv run python tools/oo_score.py src/quarry/ --threshold
	-uv run mypy src/ tests/
	-uv run ruff format --check .
	-uv run ruff check --preview --select PLR6301,PLR0913,UP035,UP040,UP007,N,I,SIM,C1901,S101 .
	-uv run pyright src/ tests/
	-uv run pytest
	@echo "Report complete."

check-full: check test-wheel ## Full quality gate including wheel test

format: ## Auto-format code
	uv run ruff format .
	uv run ruff check --fix .

install: build ## Build and install wheel locally for manual testing
	uv tool install --force dist/*.whl

build: ## Build wheel and sdist
	rm -rf dist/
	uv build
	uvx twine check dist/*

test-wheel: build ## Test the built wheel in an isolated venv on port 8422
	bash scripts/test-wheel.sh

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
	uv run pytest --cov=quarry --cov-report=html --cov-report=term-missing
	@echo "HTML report: htmlcov/index.html"
