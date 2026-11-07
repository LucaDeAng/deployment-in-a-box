"""The integration layer's guarantees, tested through a real MCP client."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from deployment_in_a_box.backend import BrezzaluceBackend, FaultPolicy
from deployment_in_a_box.mcp_server import build_server


def call(server, name: str, args: dict) -> dict:
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(name, args)
            if result.structured_content is not None:
                data = result.structured_content
                return data.get("result", data) if set(data) == {"result"} else data
            return json.loads(result.content[0].text)
    return anyio.run(go)


@pytest.fixture
def stack():
    return build_server(BrezzaluceBackend())


def test_lists_the_five_tools(stack):
    server, _ = stack

    async def go():
        async with Client(server) as client:
            return sorted(t.name for t in (await client.list_tools()).tools)
    assert anyio.run(go) == ["get_bill", "report_outage", "submit_meter_reading", "transfer_to_human", "verify_customer"]


def test_bill_needs_a_token_and_the_token_decides_the_customer(stack):
    server, _ = stack
    assert call(server, "get_bill", {"session_token": "C-2077|9999999999|forged"})["error"] == "invalid_token"
    ok = call(server, "verify_customer", {"customer_id": "c-1042", "birth_date": "1985-04-12"})
    assert ok["verified"] and ok["first_name"] == "Marco"
    bill = call(server, "get_bill", {"session_token": ok["session_token"]})
    assert bill["amount_eur"] == 84.30
    # The IBAN never leaves the integration layer in full.
    assert bill["iban_last4"] == "3456" and "iban" not in bill


def test_tampering_with_the_customer_in_a_token_fails(stack):
    server, _ = stack
    token = call(server, "verify_customer", {"customer_id": "C-1042", "birth_date": "1985-04-12"})["session_token"]
    _, expires, sig = token.split("|")
    assert call(server, "get_bill", {"session_token": f"C-2077|{expires}|{sig}"})["error"] == "invalid_token"


def test_expired_token_asks_for_reverification():
    now = [1_000_000.0]
    backend = BrezzaluceBackend(clock=lambda: now[0])
    server, _ = build_server(backend)
    token = call(server, "verify_customer", {"customer_id": "C-1042", "birth_date": "1985-04-12"})["session_token"]
    now[0] += 16 * 60
    assert call(server, "get_bill", {"session_token": token})["error"] == "token_expired"


def test_three_wrong_dates_lock_the_account_without_revealing_who_exists(stack):
    server, _ = stack
    unknown = call(server, "verify_customer", {"customer_id": "C-9999", "birth_date": "1990-01-01"})
    wrong = call(server, "verify_customer", {"customer_id": "C-2077", "birth_date": "1990-01-01"})
    assert unknown.keys() == wrong.keys()  # same shape: no account enumeration
    call(server, "verify_customer", {"customer_id": "C-2077", "birth_date": "1990-01-02"})
    last = call(server, "verify_customer", {"customer_id": "C-2077", "birth_date": "1990-01-03"})
    assert last == {"verified": False, "attempts_left": 0}
    locked = call(server, "verify_customer", {"customer_id": "C-2077", "birth_date": "1992-03-15"})
    assert locked["error"] == "account_locked" and locked["retryable"] is False


def test_validation_errors_tell_the_agent_what_to_ask(stack):
    server, _ = stack
    assert call(server, "verify_customer", {"customer_id": "1042", "birth_date": "1985-04-12"})["error"] == "invalid_customer_id"
    assert call(server, "verify_customer", {"customer_id": "C-1042", "birth_date": "12/04/1985"})["error"] == "invalid_birth_date"
    assert call(server, "report_outage", {"address": "via Roma, Torino", "kind": "electricity", "danger": False})["error"] == "address_incomplete"
    token = call(server, "verify_customer", {"customer_id": "C-1042", "birth_date": "1985-04-12"})["session_token"]
    assert call(server, "submit_meter_reading", {"session_token": token, "reading": 100})["error"] == "reading_below_previous"
    assert call(server, "submit_meter_reading", {"session_token": token, "reading": 99999})["error"] == "reading_implausible"
    assert call(server, "submit_meter_reading", {"session_token": token, "reading": 45231})["accepted"] is True


def test_gas_danger_is_p1_and_carries_the_emergency_line(stack):
    server, backend = stack
    ticket = call(server, "report_outage", {"address": "via Garibaldi 8, Bologna", "kind": "gas", "danger": True})
    assert ticket["priority"] == "P1" and ticket["emergency_line"] == "800 000 999"
    assert backend.outages[-1]["danger"] is True


def test_handoff_requires_a_useful_summary(stack):
    server, backend = stack
    assert call(server, "transfer_to_human", {"reason": "customer_request", "summary": "operatore"})["error"] == "summary_too_short"
    ok = call(server, "transfer_to_human", {"reason": "auth_failed", "summary": "Due verifiche fallite su richiesta bolletta."})
    assert ok["transferred"] and backend.transfers[-1]["reason"] == "auth_failed"


def test_injected_faults_are_retryable():
    server, _ = build_server(BrezzaluceBackend(faults=FaultPolicy(error_rate=1.0)))
    out = call(server, "report_outage", {"address": "via Roma 12, Torino", "kind": "electricity", "danger": False})
    assert out == {"error": "backend_unavailable", "message": "report_outage: back-office timeout", "retryable": True}


def test_every_call_is_audited(stack):
    server, backend = stack
    call(server, "verify_customer", {"customer_id": "C-1042", "birth_date": "1985-04-12"})
    call(server, "report_outage", {"address": "via Roma 12", "kind": "electricity", "danger": False})
    assert [a["op"] for a in backend.audit] == ["verify", "report_outage"]
