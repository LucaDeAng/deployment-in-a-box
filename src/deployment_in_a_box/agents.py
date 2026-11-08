"""The voice agent, in two interchangeable implementations.

- `RulesAgent`: a deterministic policy. It makes the whole pipeline (MCP tools,
  simulation, KPIs) reproducible in CI without an API key. It is the baseline the
  live agent has to beat, not a claim about how good an LLM agent is.
- `ClaudeAgent`: Claude with the same system prompt, taking its tools from the
  MCP server's own list, so the tool contract has one source of truth.

Both expose `turn(caller_text, tools) -> AgentTurn`, where `tools` executes a
call on the MCP server and returns its result dict.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from deployment_in_a_box.scenarios import norm

ToolExec = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
SYSTEM_PROMPT = (Path(__file__).resolve().parent / "system_prompt.md").read_text(encoding="utf-8")
MONTHS = {m: i for i, m in enumerate(
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split(), 1)}


@dataclass
class AgentTurn:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {name, args, result}
    transferred: bool = False


# --- parsing helpers ---------------------------------------------------------

def _customer_id(t: str) -> str | None:
    m = re.search(r"\bC-\d{4}\b", t, re.I)
    return m.group(0).upper() if m else None


def _birth_date(t: str) -> str | None:
    if m := re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", norm(t)):
        d, mo, y = m.groups()
        return f"{y}-{MONTHS[mo]:02d}-{int(d):02d}"
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", t):
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def _reading(t: str) -> int | None:
    m = re.search(r"(?<![\w-])(\d{5,7})(?!\d)", t)
    return int(m.group(1)) if m else None


def _spoken_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(d)} {list(MONTHS)[int(m) - 1]} {y}"


def _euro(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


class RulesAgent:
    """Deterministic policy over Italian keywords, with real tool calls and error handling."""

    def __init__(self) -> None:
        self.intent: str | None = None  # bill | meter | outage | gas
        self.customer_id: str | None = None
        self.token: str | None = None
        self.reading: int | None = None
        self.street: str | None = None
        self.city: str | None = None
        self.auth_fails = 0
        self.calls: list[dict[str, Any]] = []

    async def _call(self, tools: ToolExec, name: str, args: dict[str, Any], retries: int = 1) -> dict[str, Any]:
        result = await tools(name, args)
        self.calls.append({"name": name, "args": args, "result": result})
        if result.get("retryable") and retries:
            return await self._call(tools, name, args, retries - 1)
        return result

    async def _transfer(self, tools: ToolExec, reason: str, summary: str, say: str) -> AgentTurn:
        await self._call(tools, "transfer_to_human", {"reason": reason, "summary": summary})
        return self._turn(say, transferred=True)

    def _turn(self, text: str, transferred: bool = False) -> AgentTurn:
        calls, self.calls = self.calls, []
        return AgentTurn(text, calls, transferred)

    async def turn(self, caller_text: str, tools: ToolExec) -> AgentTurn:
        t = norm(caller_text)
        self.customer_id = _customer_id(caller_text) or self.customer_id

        if self.intent is None:
            # "operatore telefonico" is a phone carrier, not a request for a human: found by the simulation.
            if re.search(r"\boperatore\b(?! telefonic)", t) or any(w in t for w in ("persona vera", "umano")):
                return await self._transfer(tools, "customer_request", "Il cliente chiede di parlare con un operatore.",
                                            "Certo, la passo subito a un collega.")
            if "gas" in t and any(w in t for w in ("odore", "puzza", "fuga")):
                self.intent = "gas"
                return self._turn("Esca dalla stanza, non usi interruttori né fiamme e chiami subito il pronto "
                                  "intervento gas 800 000 999. Mi dice l'indirizzo, con il numero civico?")
            if any(w in t for w in ("non ho corrente", "senza luce", "saltata la luce", "blackout")):
                self.intent = "outage"
                return self._turn("Mi dispiace. Mi dice l'indirizzo del guasto, con il numero civico?")
            if any(w in t for w in ("moglie", "marito", "figlio", "figlia", "per conto", "assistente automatico")):
                return self._turn("Posso dare informazioni solo al titolare del contratto, dopo la verifica con "
                                  "codice cliente e data di nascita.")
            if "autolettura" in t or "contatore" in t:
                self.intent, self.reading = "meter", _reading(caller_text)
            elif any(w in t for w in ("bolletta", "pagare", "sollecito", "importo")):
                self.intent = "bill"
            else:
                return self._turn("Posso aiutarla con bollette, guasti e autoletture di Brezzaluce Energia. "
                                  "Per altre richieste deve rivolgersi al servizio competente.")
            return self._turn("Certo. Mi dice il codice cliente e la data di nascita dell'intestatario?")

        if self.intent in {"gas", "outage"}:
            if m := re.search(r"((?:via|corso|piazza|viale)\s+[a-zà-ù' ]+?)\s*,?\s*(?:il numero e\s*)?(\d+)?\s*,\s*([a-zà-ù]+)", t):
                self.street, number, self.city = m.group(1).strip(), m.group(2), m.group(3)
            elif m := re.search(r"numero e (\d+)\s*,\s*([a-zà-ù]+)", t):
                number, self.city = m.group(1), m.group(2)
            else:
                number = None
            address = f"{self.street or ''} {number or ''}, {self.city or ''}".strip()
            result = await self._call(tools, "report_outage",
                                      {"address": address, "kind": "gas" if self.intent == "gas" else "electricity",
                                       "danger": self.intent == "gas"})
            if result.get("error") == "address_incomplete":
                return self._turn("Mi serve anche il numero civico.")
            if result.get("error"):
                # The handoff carries the data, so the operator logs the ticket without asking again.
                summary = (f"Segnalazione guasto {'GAS con pericolo' if self.intent == 'gas' else 'luce'} "
                           f"non registrata per errore di sistema. Indirizzo: {address}.")
                return await self._transfer(tools, "system_error", summary,
                                            "Non riesco a registrare la segnalazione, la passo a un collega.")
            self.intent = None
            return self._turn(f"Segnalazione registrata con numero {result['ticket']}: un tecnico arriva entro "
                              f"{result['eta_hours']} {'ora' if result['eta_hours'] == 1 else 'ore'}.")

        # account intents: bill / meter
        if self.token is None:
            dob = _birth_date(caller_text)
            if not (self.customer_id and dob):
                return self._turn("Mi servono codice cliente e data di nascita.")
            v = await self._call(tools, "verify_customer", {"customer_id": self.customer_id, "birth_date": dob})
            if v.get("retryable"):  # still down after the retry: a system problem, not the caller's mistake
                return await self._transfer(tools, "system_error", "Verifica dell'identità non eseguita per errore di sistema.",
                                            "Il sistema non risponde, la passo a un collega.")
            if not v.get("verified"):
                self.auth_fails += 1
                # Policy: two failures -> human, before the backend's third-strike lockout.
                if self.auth_fails >= 2 or v.get("error") == "account_locked" or v.get("attempts_left") == 0:
                    return await self._transfer(tools, "auth_failed", "Verifica dell'identità non riuscita due volte.",
                                                "Non riesco a verificare i dati. La passo a un collega che potrà aiutarla.")
                return self._turn("I dati non corrispondono. Può ripetere la data di nascita?")
            self.token = v["session_token"]

        if self.intent == "bill":
            bill = await self._call(tools, "get_bill", {"session_token": self.token})
            if bill.get("error"):
                return await self._transfer(tools, "system_error", "Bolletta non disponibile per errore di sistema.",
                                            "Il sistema non risponde, la passo a un collega.")
            self.intent = None
            stato = {"unpaid": "da pagare", "paid": "già pagata", "overdue": "scaduta"}[bill["status"]]
            return self._turn(f"L'ultima bolletta è di {_euro(bill['amount_eur'])} euro, scadenza {_spoken_date(bill['due_date'])}, {stato}.")

        if self.intent == "meter":
            self.reading = _reading(caller_text) if self.reading is None or _reading(caller_text) else self.reading
            r = await self._call(tools, "submit_meter_reading", {"session_token": self.token, "reading": self.reading})
            if r.get("error") in {"reading_implausible", "reading_below_previous"}:
                self.reading = None
                return self._turn("La lettura non sembra corretta. Può rileggere il contatore?")
            if r.get("error"):
                return await self._transfer(tools, "system_error", "Autolettura non registrata per errore di sistema.",
                                            "Il sistema non risponde, la passo a un collega.")
            self.intent = None
            return self._turn("Autolettura registrata, grazie.")
        return self._turn("Come posso aiutarla?")


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class ClaudeAgent:
    """Claude over the MCP tool list. Needs ANTHROPIC_API_KEY.

    `usage` adds up the tokens of every API request in the call, and records which
    model served each one (a server-side fallback can switch it), so cost per call
    comes from what was billed, not from an estimate.
    """

    def __init__(self, tool_specs: list[dict[str, Any]], model: str | None = None):
        import anthropic  # lazy: the offline path needs no SDK

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("DIAB_MODEL", "claude-opus-5-5")
        self.tools = tool_specs
        self.messages: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {**dict.fromkeys(USAGE_FIELDS, 0), "api_requests": 0, "served_by": []}

    def _count(self, resp) -> None:
        for f in USAGE_FIELDS:
            self.usage[f] += getattr(resp.usage, f, None) or 0
        self.usage["api_requests"] += 1
        if resp.model not in self.usage["served_by"]:
            self.usage["served_by"].append(resp.model)

    async def turn(self, caller_text: str, tools: ToolExec) -> AgentTurn:
        self.messages.append({"role": "user", "content": caller_text})
        made: list[dict[str, Any]] = []
        transferred = False
        for _ in range(6):  # bound the tool loop inside one caller turn
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=1024, system=SYSTEM_PROMPT, tools=self.tools,
                messages=self.messages, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            self._count(resp)
            if resp.stop_reason == "refusal":
                raise RuntimeError("model refused even after fallback")
            self.messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                return AgentTurn(text, made, transferred)
            results = []
            for u in uses:
                out = await tools(u.name, dict(u.input))
                made.append({"name": u.name, "args": dict(u.input), "result": out})
                transferred |= u.name == "transfer_to_human" and bool(out.get("transferred"))
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out, ensure_ascii=False)})
            self.messages.append({"role": "user", "content": results})
        return AgentTurn("", made, transferred)
