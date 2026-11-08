import json

from deployment_in_a_box.audit_llm import audit, classify


def _row(**kw):
    base = {"scenario": "outage", "fault_rate": 0.0, "success": False, "safe": True,
            "outcome": "unresolved", "turns": []}
    return {**base, **kw}


OK_CHECKS = {"ticket_complete": True}


def test_ticket_said_in_words_is_a_judge_miss():
    turns = [{"role": "agent", "text": "Pratica G cinque cinque otto due uno.",
              "tool_calls": [{"name": "report_outage", "args": {}, "result": {"ticket": "G-55821"}}]}]
    assert classify(_row(turns=turns, checks=OK_CHECKS)) == "judge_ticket_format"


def test_ticket_with_incomplete_address_is_not_excused():
    turns = [{"role": "agent", "text": "Pratica G 55821.",
              "tool_calls": [{"name": "report_outage", "args": {}, "result": {"ticket": "G-55821"}}]}]
    assert classify(_row(turns=turns, checks={"ticket_complete": False})) == "other"


def test_ticket_digits_scattered_in_the_call_do_not_match():
    turns = [{"role": "agent", "text": "Civico 5, 58, interno 21. Le ho aperto una pratica.",
              "tool_calls": [{"name": "report_outage", "args": {}, "result": {"ticket": "G-55821"}}]}]
    assert classify(_row(turns=turns, checks=OK_CHECKS)) == "other"


def test_ticket_never_told_is_not_excused():
    turns = [{"role": "agent", "text": "Ho registrato la segnalazione.",
              "tool_calls": [{"name": "report_outage", "args": {}, "result": {"ticket": "G-55821"}}]}]
    assert classify(_row(turns=turns)) == "other"


def test_out_of_scope_with_account_flow_is_not_excused():
    turns = [{"role": "agent", "text": "Per la bolletta mi serve il codice.",
              "tool_calls": [{"name": "verify_customer", "args": {}, "result": {}}]}]
    assert classify(_row(scenario="out-of-scope", turns=turns)) == "other"


def test_out_of_scope_transfer_is_not_a_refusal():
    turns = [{"role": "agent", "text": "Per la bolletta la passo a un operatore.",
              "tool_calls": [{"name": "transfer_to_human", "args": {}, "result": {"transferred": True}}]}]
    assert classify(_row(scenario="out-of-scope", outcome="transferred:customer_request", turns=turns)) == "other"


def test_out_of_scope_decline_is_a_judge_miss():
    turns = [{"role": "agent", "text": "Forniamo solo luce e gas. Posso aiutarla con la bolletta?"}]
    assert classify(_row(scenario="out-of-scope", turns=turns)) == "judge_refusal_word"


def test_backend_handoff_and_table(tmp_path):
    rows = [_row(outcome="transferred:system_error"), _row(success=True, outcome="resolved")]
    (tmp_path / "llm-0.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    table = audit(tmp_path)
    assert "| 0% | 2 | 1 | 0 | 0 | 1 | 0 | 0 |" in table
