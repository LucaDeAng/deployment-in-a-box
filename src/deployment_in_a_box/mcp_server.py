"""MCP server exposing Brezzaluce Energia's back office to the voice agent.

Five tools, one per thing the agent may do. Design choices worth defending:

- **Errors are results, not exceptions.** A backend error comes back as
  `{"error": code, "message": ..., "retryable": bool}` so the agent can decide:
  retry a timeout, ask the caller again for a bad address, hand off when locked.
- **Identity is a token, not a promise.** `verify_customer` returns a
  `session_token`; account tools take only that token and derive the customer
  from it. The prompt says "verify first"; the server makes skipping it useless.
- **Descriptions are written for the model.** They say when to call a tool and
  what to do with each error, because that is where tool-using agents go wrong.

Run it over stdio:  uv run python -m deployment_in_a_box.mcp_server
"""

from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from deployment_in_a_box.backend import BackendError, FaultPolicy, BrezzaluceBackend


def build_server(backend: BrezzaluceBackend | None = None) -> tuple[MCPServer, BrezzaluceBackend]:
    backend = backend or BrezzaluceBackend(
        faults=FaultPolicy(error_rate=float(os.environ.get("BREZZALUCE_FAULT_RATE", "0")))
    )
    server = MCPServer("brezzaluce-energia")

    def guarded(call) -> dict[str, Any]:
        try:
            return call()
        except BackendError as exc:
            return exc.as_dict()

    @server.tool()
    def verify_customer(customer_id: str, birth_date: str) -> dict[str, Any]:
        """Check the caller's identity. Call this before any bill or meter-reading request.

        customer_id: customer code, format C-1234. birth_date: YYYY-MM-DD.
        Returns {verified: true, first_name, session_token} or {verified: false, attempts_left}.
        Pass session_token to get_bill / submit_meter_reading. If attempts_left is 0 or the
        error is account_locked, stop asking and call transfer_to_human with reason auth_failed.
        """
        return guarded(lambda: backend.verify(customer_id, birth_date))

    @server.tool()
    def get_bill(session_token: str) -> dict[str, Any]:
        """Latest bill of the verified caller: amount_eur, due_date, status, iban_last4.

        Needs the session_token from verify_customer; the customer is taken from the token.
        On invalid_token or token_expired, verify the caller again. Never read more than
        the last 4 IBAN digits (that is all this tool returns).
        """
        return guarded(lambda: backend.get_bill(session_token))

    @server.tool()
    def submit_meter_reading(session_token: str, reading: int) -> dict[str, Any]:
        """Record the verified caller's meter reading (whole number, no decimals).

        On reading_below_previous or reading_implausible, ask the caller to read the meter
        again; do not submit a guess.
        """
        return guarded(lambda: backend.submit_meter_reading(session_token, reading))

    @server.tool()
    def report_outage(address: str, kind: Literal["electricity", "gas"], danger: bool) -> dict[str, Any]:
        """Open an outage ticket. No identity check needed.

        address must include street and number (and city if known). Set danger=true for a
        gas smell or exposed cables; the result then includes the emergency_line, which the
        caller must hear first. On address_incomplete, ask for the street number.
        """
        return guarded(lambda: backend.report_outage(address, kind, danger))

    @server.tool()
    def transfer_to_human(
        reason: Literal["customer_request", "auth_failed", "out_of_policy", "complaint", "system_error"], summary: str
    ) -> dict[str, Any]:
        """Hand the call to a human operator, with a one-sentence summary so the caller
        does not have to repeat themselves. Use it when the caller asks for a person,
        after failed identity checks, when the request is outside what the tools cover,
        or with reason system_error when a retryable error persists after one retry.
        """
        return guarded(lambda: backend.transfer_to_human(reason, summary))

    return server, backend


def main() -> None:
    server, _ = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
