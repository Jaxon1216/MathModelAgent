.PHONY: lint format test typecheck check eval

lint:
	uv run ruff check app/

format:
	uv run ruff format app/

test:
	uv run pytest app/tests/ -v

typecheck:
	npx pyright app/

check: lint test

eval:
	uv run python scripts/eval_task.py --task-id $(TASK_ID) \
	  --baseline fixtures/baseline/2024高教杯C题/expected.json \
	  --save-scorecard
