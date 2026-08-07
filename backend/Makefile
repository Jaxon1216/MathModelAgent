.PHONY: lint format test typecheck check

lint:
	uv run ruff check app/

format:
	uv run ruff format app/

test:
	uv run pytest app/tests/ -v

typecheck:
	npx pyright app/

check: lint test
