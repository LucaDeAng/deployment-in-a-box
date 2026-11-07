"""Simulated back-office APIs of Brezzaluce Energia, a fictional Italian utility.

This plays the part of the customer's enterprise systems (CRM, billing, field
service, contact-center queue). It is deliberately unglamorous: tokens expire,
inputs are validated, some calls fail, everything is audited. Those are the
things that break real deployments, so they are the things worth simulating.

Guardrails live here, not only in the agent's prompt:
- account data needs a session token issued by `verify`; the customer is
  derived from the token, so a valid token can never read someone else's bill;
- the IBAN leaves this layer masked (last 4 digits only);
- three wrong birth dates lock the account for the rest of the session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

TOKEN_TTL_SECONDS = 15 * 60
MAX_FAILED_VERIFICATIONS = 3
EMERGENCY_LINE = "800 000 999"


class BackendError(Exception):
    """An error the agent must handle. `retryable` tells it whether trying again can help."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "retryable": self.retryable}


@dataclass
class Customer:
    customer_id: str
    first_name: str
    birth_date: str  # ISO
    iban: str
    bill: dict[str, Any]
    last_reading: int


def _seed_customers() -> dict[str, Customer]:
    rows = [
        Customer("C-1042", "Marco", "1985-04-12", "IT60X0542811101000000123456",
                 {"amount_eur": 84.30, "due_date": "2026-11-20", "status": "unpaid"}, 45120),
        Customer("C-2077", "Giulia", "1992-03-15", "IT02L1234512345123456789012",
                 {"amount_eur": 61.75, "due_date": "2026-11-18", "status": "paid"}, 18990),
        Customer("C-3310", "Antonio", "1958-09-01", "IT40S0300203280000400162854",
                 {"amount_eur": 132.10, "due_date": "2026-10-30", "status": "overdue"}, 77304),
    ]
    return {c.customer_id: c for c in rows}


@dataclass
class FaultPolicy:
    """Seeded failure injection: a share of calls fail with a retryable 503."""

    error_rate: float = 0.0
    seed: int = 7
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def maybe_fail(self, operation: str) -> None:
        if self.error_rate and self._rng.random() < self.error_rate:
            raise BackendError("backend_unavailable", f"{operation}: billing system timeout", retryable=True)


class BrezzaluceBackend:
    def __init__(self, faults: FaultPolicy | None = None, secret: bytes | None = None, clock=time.time):
        self.customers = _seed_customers()
        self.faults = faults or FaultPolicy()
        self._secret = secret or secrets.token_bytes(32)
        self._clock = clock
        self.failed_verifications: dict[str, int] = {}
        self.outages: list[dict[str, Any]] = []
        self.readings: list[dict[str, Any]] = []
        self.transfers: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self._ticket_seq = 55820

    # --- plumbing -----------------------------------------------------------

    def _log(self, operation: str, ok: bool, detail: dict[str, Any]) -> None:
        self.audit.append({"t": round(self._clock(), 3), "op": operation, "ok": ok, **detail})

    def _issue_token(self, customer_id: str) -> str:
        payload = f"{customer_id}|{int(self._clock()) + TOKEN_TTL_SECONDS}"
        sig = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{payload}|{sig}"

    def _customer_from_token(self, token: str) -> Customer:
        try:
            customer_id, expires, sig = token.split("|")
        except ValueError:
            raise BackendError("invalid_token", "session token is malformed; verify the customer first") from None
        expected = hmac.new(self._secret, f"{customer_id}|{expires}".encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            raise BackendError("invalid_token", "session token is not valid; verify the customer first")
        if int(expires) < self._clock():
            raise BackendError("token_expired", "session expired; verify the customer again")
        return self.customers[customer_id]

    # --- operations -----------------------------------------------------------

    def verify(self, customer_id: str, birth_date: str) -> dict[str, Any]:
        customer_id = customer_id.strip().upper()
        if not re.fullmatch(r"C-\d{4}", customer_id):
            self._log("verify", False, {"reason": "bad_format"})
            raise BackendError("invalid_customer_id", "customer code must look like C-1234")
        try:
            date.fromisoformat(birth_date)
        except ValueError:
            self._log("verify", False, {"reason": "bad_date"})
            raise BackendError("invalid_birth_date", "birth date must be YYYY-MM-DD") from None
        if self.failed_verifications.get(customer_id, 0) >= MAX_FAILED_VERIFICATIONS:
            self._log("verify", False, {"customer_id": customer_id, "reason": "locked"})
            raise BackendError("account_locked", "too many failed checks; only an operator can help now")
        self.faults.maybe_fail("verify")
        customer = self.customers.get(customer_id)
        if customer is None or customer.birth_date != birth_date:
            self.failed_verifications[customer_id] = self.failed_verifications.get(customer_id, 0) + 1
            left = MAX_FAILED_VERIFICATIONS - self.failed_verifications[customer_id]
            self._log("verify", False, {"customer_id": customer_id, "reason": "mismatch"})
            # Same answer for "unknown code" and "wrong date": don't leak which customers exist.
            return {"verified": False, "attempts_left": max(left, 0)}
        self.failed_verifications.pop(customer_id, None)
        self._log("verify", True, {"customer_id": customer_id})
        return {"verified": True, "first_name": customer.first_name, "session_token": self._issue_token(customer_id)}

    def get_bill(self, session_token: str) -> dict[str, Any]:
        customer = self._customer_from_token(session_token)
        self.faults.maybe_fail("get_bill")
        self._log("get_bill", True, {"customer_id": customer.customer_id})
        return {**customer.bill, "iban_last4": customer.iban[-4:]}

    def submit_meter_reading(self, session_token: str, reading: int) -> dict[str, Any]:
        customer = self._customer_from_token(session_token)
        if reading < customer.last_reading:
            self._log("submit_meter_reading", False, {"customer_id": customer.customer_id, "reason": "below_last"})
            raise BackendError("reading_below_previous",
                               f"reading is lower than the last one on file ({customer.last_reading}); ask the caller to check")
        if reading - customer.last_reading > 5000:
            self._log("submit_meter_reading", False, {"customer_id": customer.customer_id, "reason": "implausible"})
            raise BackendError("reading_implausible", "reading is far above normal consumption; ask the caller to read it again")
        self.faults.maybe_fail("submit_meter_reading")
        customer.last_reading = reading
        self.readings.append({"customer_id": customer.customer_id, "reading": reading})
        self._log("submit_meter_reading", True, {"customer_id": customer.customer_id})
        return {"accepted": True, "reading": reading}

    def report_outage(self, address: str, kind: str, danger: bool) -> dict[str, Any]:
        if kind not in {"electricity", "gas"}:
            raise BackendError("invalid_kind", "kind must be electricity or gas")
        if not re.search(r"\d", address):
            self._log("report_outage", False, {"reason": "no_street_number"})
            raise BackendError("address_incomplete", "address needs a street number; ask the caller for it")
        self.faults.maybe_fail("report_outage")
        self._ticket_seq += 1
        priority = "P1" if danger else "P3"
        ticket = {"ticket": f"G-{self._ticket_seq}", "priority": priority, "eta_hours": 1 if danger else 4}
        self.outages.append({"address": address, "kind": kind, "danger": danger, **ticket})
        self._log("report_outage", True, {"kind": kind, "danger": danger, "priority": priority})
        if danger:
            ticket["emergency_line"] = EMERGENCY_LINE
        return ticket

    def transfer_to_human(self, reason: str, summary: str) -> dict[str, Any]:
        allowed = {"customer_request", "auth_failed", "out_of_policy", "complaint", "system_error"}
        if reason not in allowed:
            raise BackendError("invalid_reason", f"reason must be one of {sorted(allowed)}")
        if len(summary.split()) < 3:
            raise BackendError("summary_too_short", "write a one-sentence summary so the caller does not repeat themselves")
        self.transfers.append({"reason": reason, "summary": summary})
        self._log("transfer_to_human", True, {"reason": reason})
        return {"transferred": True, "queue": "billing" if "bollett" in summary.lower() else "general",
                "estimated_wait_minutes": 3}

    def snapshot(self) -> str:
        return json.dumps({"outages": self.outages, "readings": self.readings, "transfers": self.transfers},
                          ensure_ascii=False)
