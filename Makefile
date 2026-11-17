.PHONY: setup seed serve test lint eval verify-audit clean

UV ?= uv

setup:
	$(UV) sync --all-groups

seed:
	$(UV) run python -m dataset.seed

serve:
	./scripts/start_local.sh

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check .
	$(UV) run mypy contracts dataset services evals tests

eval:
	$(UV) run python -m evals.runner --models offline/planner-v1 offline/baseline-lite

verify-audit:
	$(UV) run python -c "import httpx, sys; r = httpx.get('http://127.0.0.1:8000/v1/audit/verify', headers={'X-API-Key': 'havells-admin-key'}, timeout=10.0); print(r.text); sys.exit(0 if r.status_code == 200 and r.json().get('valid') else 1)"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache data/traces/* data/sovereign.db
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
