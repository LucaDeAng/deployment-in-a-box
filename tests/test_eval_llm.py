"""The LLM eval runner, without the API: a stand-in agent, synthetic rows, the pass^k math and cost rules."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from deployment_in_a_box import eval_llm, simulate
from deployment_in_a_box.agents import USAGE_FIELDS, ClaudeAgent, RulesAgent
from deployment_in_a_box.eval_llm import call_cost_usd, pass_hat_k, render, summarize

# --- pass^k ----------------------------------------------------------------------

@pytest.mark.parametrize("n,c,k,expected", [
    (5, 5, 5, 1.0),
    (5, 4, 5, 0.0),       # one failure in five: never reliable over five tries
    (5, 4, 1, 0.8),       # pass^1 is the success rate
    (5, 3, 2, 0.3),       # C(3,2) / C(5,2) = 3 / 10
    (5, 0, 1, 0.0),
])
def test_pass_hat_k_is_the_tau_bench_estimator(n, c, k, expected):
    assert pass_hat_k(n, c, k) == pytest.approx(expected)


def test_pass_hat_k_needs_at_least_k_trials():
    assert pass_hat_k(3, 3, 5) is None


def test_pass_hat_k_never_exceeds_pass_1():
    for c in range(6):
        assert pass_hat_k(5, c, 5) <= pass_hat_k(5, c, 1)


# --- cost ------------------------------------------------------------------------

def usage(model="claude-opus-5-5", inp=10_000, out=500, cw=0, cr=0, served=None):
    return {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "api_requests": 3, "served_by": served or [model]}


def test_cost_is_tokens_times_list_price():
    row = {"model": "claude-opus-5-5", "usage": usage(inp=1_000_000, out=100_000)}
    assert call_cost_usd(row) == pytest.approx(4.00 + 2.00)


def test_unpriced_model_gives_no_cost_not_a_guess():
    row = {"model": "claude-unknown-9", "usage": usage(model="claude-unknown-9")}
    assert call_cost_usd(row) is None


def test_a_fallback_model_in_the_call_gives_no_cost():
    row = {"model": "claude-opus-5-5", "usage": usage(served=["claude-opus-5-5", "claude-opus-5"])}
    assert call_cost_usd(row) is None


def test_tokens_of_a_kind_without_a_price_give_no_cost():
    prices = {"m": {"input": 1.0, "output": 2.0}}  # no cache prices
    assert call_cost_usd({"model": "m", "usage": usage(model="m")}, prices) is not None
    assert call_cost_usd({"model": "m", "usage": usage(model="m", cr=100)}, prices) is None


def test_rules_agent_rows_have_no_cost():
    assert call_cost_usd({"scenario": "bill"}) is None


def test_claude_agent_adds_up_usage_across_requests():
    agent = ClaudeAgent.__new__(ClaudeAgent)  # no client: only the counter is under test
    agent.usage = {**dict.fromkeys(USAGE_FIELDS, 0), "api_requests": 0, "served_by": []}
    for n in (100, 200):
        agent._count(SimpleNamespace(model="claude-opus-5-5", usage=SimpleNamespace(
            input_tokens=n, output_tokens=10, cache_creation_input_tokens=None, cache_read_input_tokens=None)))
    assert agent.usage["input_tokens"] == 300 and agent.usage["output_tokens"] == 20
    assert agent.usage["api_requests"] == 2 and agent.usage["served_by"] == ["claude-opus-5-5"]


# --- summary and report from synthetic rows ---------------------------------------

def row(sid, seed, success, *, expected="resolved", outcome=None, safe=True, weight=0.5, model="claude-opus-5-5"):
    return {"scenario": sid, "seed": seed, "weight": weight, "expected": expected, "success": success,
            "outcome": outcome or (expected if success else "unresolved"), "safe": safe,
            "checks": {"emergency_first": safe}, "n_turns": 2, "turns": [sid, seed], "fault_rate": 0.0,
            "model": model, "usage": usage(model=model)}


def test_summary_reuses_the_kpi_tree_and_adds_pass_k():
    rows = ([row("bill", t * 100, success=t != 0) for t in range(5)]                     # 4/5
            + [row("human", t * 100 + 7, True, expected="transferred:customer_request") for t in range(5)])
    s = summarize(rows, k=5)
    assert s["task_success"] == pytest.approx(0.9)          # weighted, from kpi.compute
    assert s["handoff_rate"] == pytest.approx(0.5) and s["containment"] == pytest.approx(0.5)
    assert s["pass_1"] == pytest.approx((0.8 + 1.0) / 2)    # unweighted over scenarios
    assert s["pass_k"] == pytest.approx((0.0 + 1.0) / 2)
    assert s["per_scenario"]["bill"]["outcomes"] == {"resolved": 4, "unresolved": 1}
    assert s["cost_per_call_usd"] == pytest.approx(10_000 * 4 / 1e6 + 500 * 20 / 1e6)


def test_safety_violations_are_counted_per_call():
    rows = [row("gas", 0, True, safe=False), row("gas", 100, True), row("gas", 200, True, safe=False)]
    s = summarize(rows, k=3)
    assert s["safety_violations"] == 2
    assert s["safety_failures"] == ["gas:emergency_first"]


def test_report_shows_tokens_only_when_cost_is_unknown():
    rows = [row("bill", t * 100, True, model="claude-unknown-9") for t in range(2)]
    text = render({0.0: summarize(rows, k=2)}, [0.0, 0.5], 2, "claude-unknown-9")
    line = next(x for x in text.splitlines() if x.startswith("| 0% |"))
    assert "n/a (tokens only)" in line and "10,000" in line
    assert "| 50% | not run yet |" in text


def test_template_says_not_run_yet_with_the_expected_call_count():
    text = render({}, [0.0, 0.2, 0.5], 5, "claude-opus-5-5")
    assert "Not run yet" in text and "165 simulated calls" in text
    assert "| pass^1 | pass^5 |" in text and "| 20% | not run yet |" in text


# --- the runner end to end, with a stand-in for Claude ----------------------------

class FakeClaude(RulesAgent):
    """The rules agent wearing ClaudeAgent's interface: same trace fields, no API."""

    made = 0

    def __init__(self, tool_specs, model=None):
        super().__init__()
        assert {t["name"] for t in tool_specs} >= {"verify_customer", "transfer_to_human"}
        FakeClaude.made += 1
        self.model = model or "claude-opus-5-5"
        self.usage = usage(model=self.model, inp=1_000, out=100)


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(simulate, "ClaudeAgent", FakeClaude)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    FakeClaude.made = 0
    return FakeClaude


def test_runner_writes_jsonl_and_report_and_skips_finished_calls(fake_claude, tmp_path):
    def args(trials):
        return ["--scenarios", "bill", "human", "--fault-rates", "0", "0.5", "--trials", str(trials),
                "--runs-dir", str(tmp_path / "runs"), "--report", str(tmp_path / "llm-results.md")]

    eval_llm.main(args(2))
    assert fake_claude.made == 8
    rows = [json.loads(x) for x in (tmp_path / "runs" / "llm-0.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(r["scenario"], r["seed"]) for r in rows} == {("bill", 0), ("bill", 100), ("human", 7), ("human", 107)}
    assert all(r["usage"]["input_tokens"] == 1_000 and r["model"] == "claude-opus-5-5" for r in rows)

    eval_llm.main(args(2))  # everything on disk: no new calls
    assert fake_claude.made == 8
    eval_llm.main(args(3))  # one more trial: only the new seeds run
    assert fake_claude.made == 12

    report = (tmp_path / "llm-results.md").read_text(encoding="utf-8")
    assert "| 0% | 6 | 100% | 0 |" in report
    assert "| bill | resolved | 50% |" in report and "Not run yet" not in report


def test_runner_dry_run_makes_no_calls(fake_claude, tmp_path, capsys):
    eval_llm.main(["--dry-run", "--runs-dir", str(tmp_path)])
    assert fake_claude.made == 0
    assert "165 simulated calls in the grid, 0 already on disk, 165 to run" in capsys.readouterr().out


def test_api_key_falls_back_to_the_secrets_file_and_is_never_printed(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / "anthropic.env"
    f.write_text("# comment\nexport ANTHROPIC_API_KEY='sk-test-123'\n", encoding="utf-8")
    eval_llm.ensure_api_key(f)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test-123"
    assert "sk-test-123" not in capsys.readouterr().out


def test_missing_api_key_stops_before_any_call(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        eval_llm.ensure_api_key(tmp_path / "missing.env")
