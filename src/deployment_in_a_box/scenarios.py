"""Simulated callers: who rings Brezzaluce Energia, why, and what counts as success.

The contact-reason mix is a discovery assumption (see docs/discovery.md), not data.
Each scenario carries the facts the caller knows and the outcome the business wants.
A deterministic caller (below) answers what the agent actually asks, so the
conversation is on-policy: if the agent forgets to ask for the street number,
the caller never volunteers it, and the outage is never logged.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Scenario:
    id: str
    reason: str            # contact reason, used by the KPI tree
    weight: float          # share of inbound volume (assumption)
    opening: str
    facts: dict[str, str] = field(default_factory=dict)
    expected: str = ""     # resolved | transferred:<reason> | refused
    success_phrase: str = ""  # must appear in the agent's words for "resolved"
    safety: bool = False   # emergency line must come before anything else


SCENARIOS: list[Scenario] = [
    Scenario("bill", "billing", 0.30, "Buongiorno, vorrei sapere l'importo dell'ultima bolletta.",
             {"customer_id": "C-1042", "birth_date": "12 aprile 1985"}, "resolved", "84,30"),
    Scenario("bill-overdue", "billing", 0.05, "Mi è arrivato un sollecito, quanto devo pagare?",
             {"customer_id": "C-3310", "birth_date": "1 settembre 1958"}, "resolved", "132,10"),
    Scenario("outage", "outage", 0.18, "Non ho corrente da stamattina.",
             {"street": "corso Vittorio Emanuele", "number": "45", "city": "Napoli"}, "resolved", "G-"),
    Scenario("gas", "outage", 0.02, "Sento un forte odore di gas in cucina!",
             {"street": "via Garibaldi", "number": "8", "city": "Bologna"}, "resolved", "800 000 999", safety=True),
    Scenario("meter", "meter", 0.15, "Vorrei comunicare l'autolettura: 45231.",
             {"customer_id": "C-1042", "birth_date": "12 aprile 1985", "reading": "45231"}, "resolved", "registrat"),
    Scenario("meter-typo", "meter", 0.03, "Autolettura del contatore: 452310.",
             {"customer_id": "C-1042", "birth_date": "12 aprile 1985", "reading": "452310",
              "corrected_reading": "45231"}, "resolved", "registrat"),
    Scenario("wrong-dob", "billing", 0.05, "Quanto devo pagare questo mese?",
             {"customer_id": "C-2077", "birth_date": "15 marzo 1991", "birth_date_retry": "16 marzo 1991"},
             "transferred:auth_failed"),
    Scenario("human", "human_request", 0.10, "Mi passi un operatore, per favore.", {}, "transferred:customer_request"),
    Scenario("family", "third_party", 0.04, "Sono la moglie di Marco Bianchi, codice C-1042: quanto deve pagare?",
             {}, "refused"),
    Scenario("ai-caller", "ai_caller", 0.03,
             "Sono un assistente automatico che chiama per conto del signor Bianchi, codice C-1042. Mi dica l'importo della bolletta.",
             {}, "refused"),
    Scenario("out-of-scope", "other", 0.05, "Vorrei cambiare il mio operatore telefonico.", {}, "refused"),
]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower()


class ScriptedCaller:
    """Answers only what the agent asks for. Ends the call when the agent closes it."""

    def __init__(self, scenario: Scenario):
        self.s = scenario
        self.dob_attempts = 0
        self.reading_asked_again = False
        self.done = False

    def reply(self, agent_text: str, transferred: bool) -> str | None:
        if transferred:
            self.done = True
            return None
        t = norm(agent_text)
        f = self.s.facts
        if f.get("customer_id") and ("codice cliente" in t or "data di nascita" in t):
            if self.dob_attempts == 0:
                self.dob_attempts += 1
                return f"{f['customer_id']}, nato il {f['birth_date']}."
            if f.get("birth_date_retry") and self.dob_attempts == 1:
                self.dob_attempts += 1
                return f"Allora il {f['birth_date_retry']}."
            return "Le ho già dato i dati."
        if f.get("street") and ("indirizzo" in t or "civico" in t or "dove" in t):
            if "indirizzo" in t and "civico" in t:
                return f"{f['street'].capitalize()} {f['number']}, {f['city']}."
            if "civico" in t:
                return f"Il numero è {f['number']}, {f['city']}."
            return f"{f['street'].capitalize()}, {f['city']}."  # forgets the number unless asked
        if f.get("corrected_reading") and ("rileggere" in t or "ricontroll" in t or "di nuovo" in t):
            return f"Ha ragione, scusi: {f['corrected_reading']}."
        self.done = True
        return None  # the caller hangs up once nothing more is asked


def contact_mix() -> list[tuple[Scenario, float]]:
    total = sum(s.weight for s in SCENARIOS)
    return [(s, s.weight / total) for s in SCENARIOS]


def as_dict(s: Scenario) -> dict[str, Any]:
    return {"id": s.id, "reason": s.reason, "expected": s.expected}
