"""Claude on the same scenarios the rules agent runs, k trials each, at several backend fault rates.

    uv run python -m deployment_in_a_box.eval_llm                       # full grid: 11 scenarios x 5 trials x 3 fault rates
    uv run python -m deployment_in_a_box.eval_llm --dry-run             # what would run, and what is already done
    uv run python -m deployment_in_a_box.eval_llm --report-only         # rebuild docs/llm-results.md from runs/, no API calls

Each finished call is appended to runs/llm-<fault>.jsonl as soon as it ends, so a
crash or a Ctrl-C loses at most one call, and a rerun skips every (scenario, seed,
fault) triple that is already on disk. The scoring is the KPI tree's (kpi.compute),
the same judge and the same weights as the rules agent; this module only adds
pass^k and what the calls cost.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from math import comb
from pathlib import Path
from typing import Any

from deployment_in_a_box import kpi
from deployment_in_a_box.scenarios import SCENARIOS

DEFAULT_MODEL = os.environ.get("DIAB_MODEL", "claude-opus-5-5")  # what `--agent claude` uses (agents.ClaudeAgent)
DEFAULT_FAULT_RATES = (0.0, 0.2, 0.5)
DEFAULT_TRIALS = 5
SECRETS_FILE = Path.home() / ".secrets" / "anthropic.env"

# USD per million tokens, Anthropic first-party API list prices, read from
# https://platform.claude.com/docs/en/about-claude/pricing on 2026-11-08.
# Check that page before quoting a cost. A price that is not here is None, never a guess:
# the report then shows tokens only. Haiku 5.5 rates are for prompts up to 100,000 tokens.
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write_5m": 5.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write_5m": 2.50, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write_5m": 0.125, "cache_read": 0.01},
}
_PRICED = {"input_tokens": "input", "output_tokens": "output",
           "cache_creation_input_tokens": "cache_write_5m", "cache_read_input_tokens": "cache_read"}


# --- metrics -----------------------------------------------------------------

def pass_hat_k(n: int, c: int, k: int) -> float | None:
    """τ-bench's pass^k: the chance that k trials drawn from n all succeed, given c successes.

    Unbiased estimator C(c, k) / C(n, k). None when there are fewer than k trials.
    pass^1 is the plain success rate; pass^k <= pass^1, and the gap is the inconsistency.
    """
    if k < 1 or n < k:
        return None
    return comb(c, k) / comb(n, k)


def call_cost_usd(row: dict[str, Any], prices: dict[str, dict[str, float]] = PRICES_USD_PER_MTOK) -> float | None:
    """Billed cost of one simulated call, or None if any token in it has no known price."""
    usage = row.get("usage")
    if not usage:
        return None
    served = usage.get("served_by") or [row.get("model")]
    if len(served) != 1 or served[0] not in prices:  # a fallback model ran, or the model is unpriced
        return None
    price = prices[served[0]]
    total = 0.0
    for field, key in _PRICED.items():
        tokens = usage.get(field) or 0
        if tokens and key not in price:
            return None
        total += tokens * price.get(key, 0.0) / 1e6
    return total


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def summarize(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """One fault rate: the KPI tree's numbers, plus pass^1 / pass^k and tokens and cost per call."""
    k_tree = kpi.compute(rows)
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_scenario[r["scenario"]].append(r)
    per_scenario = {}
    for sid, rs in by_scenario.items():
        n, c = len(rs), sum(r["success"] for r in rs)
        per_scenario[sid] = {
            "expected": rs[0]["expected"], "trials": n, "successes": c,
            "pass_1": pass_hat_k(n, c, 1), "pass_k": pass_hat_k(n, c, k),
            "safety_violations": sum(not r["safe"] for r in rs),
            "outcomes": dict(Counter(r["outcome"] for r in rs).most_common()),
            "input_tokens": _mean([r["usage"]["input_tokens"] for r in rs if r.get("usage")]),
            "output_tokens": _mean([r["usage"]["output_tokens"] for r in rs if r.get("usage")]),
        }
    # τ-bench convention: pass^k is averaged over tasks, unweighted. Task success is weighted by contact mix.
    p1 = [s["pass_1"] for s in per_scenario.values()]
    pk = [s["pass_k"] for s in per_scenario.values()]
    costs = [call_cost_usd(r) for r in rows]
    usage = [r["usage"] for r in rows if r.get("usage")]
    return {
        "calls": len(rows),
        "task_success": k_tree["task_success"],
        "safety_violations": sum(not r["safe"] for r in rows),
        "safety_failures": k_tree["safety_failures"],
        "handoff_rate": 1 - k_tree["containment"],
        "containment": k_tree["containment"],
        "pass_1": _mean(p1),
        "pass_k": None if None in pk else _mean(pk),
        "input_tokens_per_call": _mean([u["input_tokens"] for u in usage]),
        "output_tokens_per_call": _mean([u["output_tokens"] for u in usage]),
        "api_requests_per_call": _mean([u["api_requests"] for u in usage]),
        "cost_per_call_usd": None if not costs or None in costs else _mean(costs),
        "per_scenario": per_scenario,
    }


# --- report ------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.0f}"


def _usd(x: float | None) -> str:
    return "n/a (tokens only)" if x is None else f"${x:.4f}"


HEADER = """# Results with a real LLM

<!-- Generated by `uv run python -m deployment_in_a_box.eval_llm`. Do not edit by hand:
     rerun the command, or `--report-only` to rebuild this file from runs/llm-*.jsonl. -->

Claude ({model}) as the phone agent, on the same {n_scen} scripted callers as the rules
agent, {k} trials per scenario, at {faults} injected backend faults. Same judge, same
safety checks, same contact-mix weights. Synthetic company and callers: this measures the
agent under this harness, not a deployment.

- **Task success, containment, handoff rate** are weighted by the assumed contact mix (kpi.py).
- **pass^1 / pass^k** follow τ-bench: per scenario, the chance that 1 (or all {k}) trials
  succeed, averaged over scenarios without weights. pass^k below pass^1 means the agent
  is right sometimes, not reliably.
- **Cost per call** is what the API billed for one simulated call (every request in its
  tool loop), at the list prices in `eval_llm.PRICES_USD_PER_MTOK`. It is the LLM only,
  no telephony or speech.
- **The callers react to exact phrases** (for example "800 000 999", "84,30"). A correct
  paraphrase can fail a check, so these numbers can understate the agent, never overstate it.
"""


def render(summaries: dict[float, dict[str, Any]], fault_rates: list[float], k: int, model: str) -> str:
    out = [HEADER.format(model=model, n_scen=len(SCENARIOS), k=k,
                         faults=", ".join(_pct(f) for f in fault_rates))]
    if not summaries:
        out.append(f"\n**Not run yet.** Expected: {len(SCENARIOS)} scenarios x {k} trials x "
                   f"{len(fault_rates)} fault rates = {len(SCENARIOS) * k * len(fault_rates)} simulated calls.\n")
    out.append("\n## By fault rate\n\n"
               f"| Backend faults | Calls | Task success | Safety violations | Handoff rate | Containment "
               f"| pass^1 | pass^{k} | Input tokens / call | Output tokens / call | Cost / call |\n"
               "|---|---|---|---|---|---|---|---|---|---|---|\n")
    for f in fault_rates:
        s = summaries.get(f)
        if s is None:
            out.append(f"| {_pct(f)} | not run yet | | | | | | | | | |\n")
            continue
        out.append(f"| {_pct(f)} | {s['calls']} | {_pct(s['task_success'])} | {s['safety_violations']} | "
                   f"{_pct(s['handoff_rate'])} | {_pct(s['containment'])} | {_pct(s['pass_1'])} | {_pct(s['pass_k'])} | "
                   f"{_num(s['input_tokens_per_call'])} | {_num(s['output_tokens_per_call'])} | "
                   f"{_usd(s['cost_per_call_usd'])} |\n")
    fails = sorted({x for s in summaries.values() for x in s["safety_failures"]})
    if summaries:
        out.append("\nSafety failures (scenario:check): " + (", ".join(fails) if fails else "none") + "\n")

    out.append("\n## By scenario\n\n"
               f"| Scenario | Expected | Backend faults | Successes | pass^1 | pass^{k} | Safety violations "
               "| Outcomes | Input tokens / call | Output tokens / call |\n"
               "|---|---|---|---|---|---|---|---|---|---|\n")
    for sc in SCENARIOS:
        for f in fault_rates:
            p = summaries.get(f, {}).get("per_scenario", {}).get(sc.id)
            if p is None:
                out.append(f"| {sc.id} | {sc.expected} | {_pct(f)} | not run yet | | | | | | |\n")
                continue
            outcomes = ", ".join(f"{o} x{n}" for o, n in p["outcomes"].items())
            out.append(f"| {sc.id} | {sc.expected} | {_pct(f)} | {p['successes']}/{p['trials']} | "
                       f"{_pct(p['pass_1'])} | {_pct(p['pass_k'])} | {p['safety_violations']} | {outcomes} | "
                       f"{_num(p['input_tokens'])} | {_num(p['output_tokens'])} |\n")
    return "".join(out)


# --- runs on disk --------------------------------------------------------------

def run_path(runs_dir: Path, fault: float) -> Path:
    return runs_dir / f"llm-{fault:g}.jsonl"


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def plan(scenario_ids: list[str], trials: int, done: list[dict[str, Any]]) -> list[tuple[Any, int]]:
    """(scenario, seed) pairs still to run. Seeds follow simulate.run_all: trial * 100 + scenario index."""
    have = {(r["scenario"], r["seed"]) for r in done}
    todo = []
    for t in range(trials):
        for i, s in enumerate(SCENARIOS):
            if s.id in scenario_ids and (s.id, t * 100 + i) not in have:
                todo.append((s, t * 100 + i))
    return todo


def ensure_api_key(secrets_file: Path = SECRETS_FILE) -> None:
    """ANTHROPIC_API_KEY from the environment, else from ~/.secrets/anthropic.env. Never printed."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    if secrets_file.exists():
        for line in secrets_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "ANTHROPIC_API_KEY" and value.strip():
                os.environ["ANTHROPIC_API_KEY"] = value.strip().strip("'\"")
                return
    raise SystemExit(f"ANTHROPIC_API_KEY is not set and not found in {secrets_file}.")


def _selected(rows: list[dict[str, Any]], scenario_ids: list[str], trials: int, model: str) -> list[dict[str, Any]]:
    """The rows this report is about: chosen scenarios, the first `trials` trials, this model."""
    return [r for r in rows if r["scenario"] in scenario_ids and r["seed"] // 100 < trials and r.get("model") == model]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fault-rates", type=float, nargs="+", default=list(DEFAULT_FAULT_RATES))
    p.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help="k: trials per scenario")
    p.add_argument("--scenarios", nargs="+", default=[s.id for s in SCENARIOS], choices=[s.id for s in SCENARIOS])
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--runs-dir", type=Path, default=Path("runs"))
    p.add_argument("--report", type=Path, default=Path("docs/llm-results.md"))
    p.add_argument("--dry-run", action="store_true", help="print what would run; no API calls")
    p.add_argument("--report-only", action="store_true", help="rebuild the report from runs on disk; no API calls")
    a = p.parse_args(argv)

    todo = {f: plan(a.scenarios, a.trials, _selected(load_rows(run_path(a.runs_dir, f)), a.scenarios, a.trials, a.model))
            for f in a.fault_rates}
    total = len(a.scenarios) * a.trials * len(a.fault_rates)
    pending = sum(len(t) for t in todo.values())
    print(f"{a.model}: {total} simulated calls in the grid, {total - pending} already on disk, {pending} to run.")
    if a.dry_run:
        return

    if not a.report_only and pending:
        import anyio

        from deployment_in_a_box.simulate import run_call

        ensure_api_key()
        for f, calls in todo.items():
            path = run_path(a.runs_dir, f)
            path.parent.mkdir(parents=True, exist_ok=True)
            for s, seed in calls:
                try:
                    row = anyio.run(run_call, s, "claude", f, seed, a.model)
                except Exception as exc:  # rate limit, refusal, network: stop, keep what is done
                    raise SystemExit(f"stopped at {s.id} seed {seed} fault {f:g}: {type(exc).__name__}: {exc}\n"
                                     "Finished calls are saved; rerun the same command to resume.") from exc
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                u = row["usage"]
                print(f"  fault {f:g} {s.id} seed {seed}: {row['outcome']}, "
                      f"{'success' if row['success'] else 'FAIL'}, {'safe' if row['safe'] else 'UNSAFE'}, "
                      f"{u['input_tokens']} in / {u['output_tokens']} out tokens, {_usd(call_cost_usd(row))}")

    summaries = {}
    for f in a.fault_rates:
        rows = _selected(load_rows(run_path(a.runs_dir, f)), a.scenarios, a.trials, a.model)
        if rows:
            summaries[f] = summarize(rows, a.trials)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(render(summaries, a.fault_rates, a.trials, a.model), encoding="utf-8")
    print(f"report -> {a.report}")
    if any(s["safety_violations"] for s in summaries.values()):
        print("safety violations found: see the report", file=sys.stderr)


if __name__ == "__main__":
    main()
