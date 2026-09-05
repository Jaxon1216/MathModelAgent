.PHONY: lint format test test-modeler smoke-modeler typecheck check eval e2e-fixture

lint:
	uv run ruff check app/ tests/ scripts/smoke_modeler.py scripts/run_fixture.py

format:
	uv run ruff format app/ tests/ scripts/

test:
	uv run pytest app/tests/ tests/ -m "not live_modeler" -v

test-modeler:
	uv run pytest \
	  tests/unit/test_model_plan.py \
	  tests/unit/test_data_catalog.py \
	  tests/unit/test_smoke_modeler.py \
	  tests/integration/test_modeler.py \
	  tests/integration/test_modeler_bridge.py \
	  -m "not live_modeler" -v

smoke-modeler:
	RUN_LIVE_MODELER=1 uv run pytest \
	  tests/live/test_modeler_live.py \
	  -m live_modeler -v -s

typecheck:
	npx pyright app/

check: lint test

eval:
	uv run python scripts/eval_task.py --task-id $(TASK_ID) \
	  --baseline fixtures/baseline/2024高教杯C题/expected.json \
	  --save-scorecard

e2e-fixture:
	uv run python scripts/run_fixture.py --source "$(or $(FIXTURE),2024高教杯C题)"
