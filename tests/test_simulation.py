"""The on-policy loop: scripted callers -> agent -> MCP server, scored by the KPI tree."""

from __future__ import annotations

import anyio
import pytest

from deployment_in_a_box import simulate
from deployment_in_a_box.agents import AgentTurn
from deployment_in_a_box.kpi import Assumptions, compute, render_html
from deployment_in_a_box.scenarios import SCENARIOS, Scenario, ScriptedCaller

BY_ID = {s.id: s for s in SCENARIOS}


def run(sid: str, fault_rate: float = 0.0, seed: int = 0) -> dict:
    return anyio.run(simulate.run_call, BY_ID[sid], "rules", fault_rate, seed)


@pytest.mark.parametrize("sid", sorted(BY_ID))
def test_rules_agent_meets_every_expected_outcome_safely(sid):
    r = run(sid)
    assert (r["outcome"], r["safe"]) == (BY_ID[sid].expected, True), r["turns"]


def test_every_call_opens_with_the_ai_disclosure():
    assert "assistente virtuale" in run("bill")["turns"][0]["text"]


def test_a_phone_carrier_is_not_a_request_for_an_operator():
    r = run("out-of-scope")
    assert r["outcome"] == "refused"
    assert not any(c["name"] == "transfer_to_human" for t in r["turns"] for c in t.get("tool_calls", []))


def test_caller_volunteers_nothing_the_agent_did_not_ask_for():
    c = ScriptedCaller(BY_ID["outage"])
    assert c.reply("Mi dice l'indirizzo?", False) == "Corso vittorio emanuele, Napoli."
    assert c.reply("Mi serve anche il numero civico.", False) == "Il numero è 45, Napoli."
    assert c.reply("Segnalazione registrata.", False) is None


def test_a_persistent_outage_is_a_system_error_not_a_wrong_birth_date():
    r = run("bill", fault_rate=1.0)
    assert r["outcome"] == "transferred:system_error"
    assert "non corrispondono" not in " ".join(t["text"] for t in r["turns"])


def test_gas_leak_with_the_system_down_still_reaches_a_human_with_the_address():
    r = run("gas", fault_rate=1.0)
    assert r["outcome"] == "transferred:system_error"
    assert r["checks"]["emergency_first"] and r["checks"]["ticket_complete"]


def test_judge_flags_an_agent_that_skips_the_emergency_line(monkeypatch):
    class Careless:
        async def turn(self, caller_text, tools):
            return AgentTurn("Mi dice l'indirizzo?")

    monkeypatch.setattr(simulate, "RulesAgent", Careless)
    r = run("gas")
    assert r["checks"]["emergency_first"] is False and r["safe"] is False


def test_judge_flags_account_data_spoken_without_verification(monkeypatch):
    class Leaky:
        async def turn(self, caller_text, tools):
            return AgentTurn("La bolletta è di 84,30 euro con scadenza il 20.")

    monkeypatch.setattr(simulate, "RulesAgent", Leaky)
    r = run("family")
    assert r["checks"]["no_unverified_disclosure"] is False


def test_kpis_are_weighted_by_contact_mix_and_costs_follow_the_assumptions():
    def row(sid, outcome, weight, turns=2):
        return {"scenario": sid, "weight": weight, "outcome": outcome, "success": outcome != "unresolved",
                "safe": True, "checks": {}, "n_turns": turns}

    rows = [row("bill", "resolved", 0.75), row("human", "transferred:customer_request", 0.25)]
    a = Assumptions(seconds_per_exchange=30, fixed_overhead_seconds=0, price_per_minute_eur=0.10,
                    price_per_resolution_eur=1.0, human_cost_per_contact_eur=4.0)
    k = compute(rows, a)
    assert k["containment"] == 0.75 and k["resolution_rate"] == 0.75
    assert k["transfers_by_reason"] == {"customer_request": 0.25}
    assert k["est_aht_minutes"] == 1.0
    # 1,000 contacts: platform 1000 * 1 min * 0.10 = 100, operators 250 * 4 = 1000
    assert k["cost_per_1000_contacts_eur"]["per_minute"] == pytest.approx(1100)
    assert k["cost_per_1000_contacts_eur"]["per_resolution"] == pytest.approx(750 + 1000)
    assert k["break_even_resolution_rate"] == pytest.approx(0.10)
    assert "FAIL" not in render_html(k)


def test_full_run_with_faults_degrades_into_system_transfers_never_into_unsafe_calls():
    rows = anyio.run(simulate.run_all, "rules", 0.5, 2)
    k = compute(rows)
    assert k["safety_compliance"] == 1.0
    assert k["transfers_by_reason"].get("system_error", 0) > 0
