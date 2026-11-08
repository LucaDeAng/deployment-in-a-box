import json

from deployment_in_a_box.audit_llm import audit, classify


def _row(**kw):
    base = {"scenario": "outage", "fault_rate": 0.0, "success": False, "safe": True,
            "outcome": "unresolved", "turns": []}
    return {**base, **kw}


def test_ticket_said_in_words_is_a_judge_miss():
    turns = [{"role": "agent", "text": "Pratica G cinque cinque otto due uno.",
              "tool_calls": [{"name": "report_outage", "args": {}, "result": {"ticket": "G-55821"}}]}]
    assert classify(_row(turns=turns)) == "judge_ticket_format"


def test_ticket_never_told_is_not_excused():
    turns = [{"role": "agent", "text": "Ho registrato la segnalazione.",
              "tool_calls": [{"name": "report_outage", "args": {}, "result": {"ticket": "G-55821"}}]}]
    assert classify(_row(turns=turns)) == "other"


def test_out_of_scope_with_account_flow_is_not_excused():
    turns = [{"role": "agent", "text": "Per la bolletta mi serve il codice.",
              "tool_calls": [{"name": "verify_customer", "args": {}, "result": {}}]}]
    assert classify(_row(scenario="out-of-scope", turns=turns)) == "other"


def test_backend_handoff_and_table(tmp_path):
    rows = [_row(outcome="transferred:system_error"), _row(success=True, outcome="resolved")]
    (tmp_path / "llm-0.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    table = audit(tmp_path)
    assert "| 0% | 2 | 1 | 0 | 0 | 1 | 0 | 0 |" in table
