.PHONY: help test lint type check format

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
