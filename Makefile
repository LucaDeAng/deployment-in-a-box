# Convenience targets. Without make (e.g. on Windows), run the commands directly.
.PHONY: test eval-llm eval-llm-dry eval-llm-report eval-llm-smoke

test:
	uv run pytest -q

# Full grid: 11 scenarios x 5 trials x faults 0, 0.2, 0.5 = 165 simulated calls. Costs real money.
eval-llm:
	uv run python -m deployment_in_a_box.eval_llm

# What would run, and what is already in runs/. No API calls.
eval-llm-dry:
	uv run python -m deployment_in_a_box.eval_llm --dry-run

# Rebuild docs/llm-results.md from runs/llm-*.jsonl. No API calls.
eval-llm-report:
	uv run python -m deployment_in_a_box.eval_llm --report-only

# One call end to end (a few cents). Writes to runs/smoke, not to docs/.
eval-llm-smoke:
	uv run python -m deployment_in_a_box.eval_llm --scenarios bill --trials 1 --fault-rates 0 \
		--runs-dir runs/smoke --report runs/smoke/llm-results.md
