.PHONY: lint format test typecheck check logs

lint:
	uv run ruff check app/

format:
	uv run ruff format app/

test:
	uv run pytest app/tests/ -v

typecheck:
	npx pyright app/

check: lint test

logs:
	uv run python scripts/preview_logs.py --latest --summary
