"""Sort every failed LLM call into why it failed, from the traces in runs/llm-*.jsonl.

    uv run python -m deployment_in_a_box.audit_llm

The judge in simulate.py checks success with exact phrases written against the rules agent
("G-" for an outage ticket, "bollette" for an out-of-scope refusal). This script reads what
the agent actually did in each failed call, so a correct paraphrase is not counted as an
agent failure. It changes no score: docs/llm-results.md keeps the judge's numbers.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

CATEGORIES = {
    "judge_ticket_format": "outage: ticket filed with the full address and its number told, "
                           "spoken as \"G 55821\" or digit by digit instead of \"G-55821\"",
    "judge_refusal_word": "out-of-scope: declined, no account flow started, said \"bolletta\" "
                          "where the judge looks for \"bollette\"",
    "handoff_backend_error": "handed off to an operator after a backend error (the designed degradation)",
    "caller_could_not_answer": "gas: the scripted caller does not understand \"in che via\", hung up; "
                               "the agent neither filed a partial ticket nor handed off",
    "other": "anything else (read the trace)",
}


def classify(r: dict) -> str:
    calls = [c for t in r["turns"] for c in t.get("tool_calls", [])]
    agent = " ".join(t["text"] for t in r["turns"] if t["role"] == "agent")
    if r["outcome"] == "transferred:system_error":
        return "handoff_backend_error"
    if r["scenario"] == "outage":
        tickets = [c["result"]["ticket"] for c in calls if c["name"] == "report_outage" and c["result"].get("ticket")]
        spoken = re.sub(r"\D", "", _digits(agent))
        if tickets and re.sub(r"\D", "", tickets[-1]) in spoken:
            return "judge_ticket_format"
    if r["scenario"] == "out-of-scope":
        account = [c for c in calls if c["name"] in {"verify_customer", "get_bill", "submit_meter_reading"}]
        if not account and "bollett" in agent.lower():
            return "judge_refusal_word"
    if r["scenario"] == "gas" and not r["safe"] and not calls:
        return "caller_could_not_answer"
    return "other"


_WORDS = {"zero": "0", "uno": "1", "due": "2", "tre": "3", "quattro": "4", "cinque": "5", "sei": "6",
          "sette": "7", "otto": "8", "nove": "9"}


def _digits(text: str) -> str:
    """'G cinque cinque otto' -> 'G 5 5 8', so a ticket number said in words still matches."""
    return re.sub(r"\b(" + "|".join(_WORDS) + r")\b", lambda m: _WORDS[m.group(1).lower()], text, flags=re.I)


def audit(runs_dir: Path) -> str:
    rows = [json.loads(line) for f in sorted(runs_dir.glob("llm-*.jsonl")) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    faults = sorted({r["fault_rate"] for r in rows})
    counts = Counter((r["fault_rate"], classify(r)) for r in rows if not r["success"])
    lines = ["| Backend faults | Calls | Judged success | " + " | ".join(CATEGORIES) + " |",
             "|---" * (3 + len(CATEGORIES)) + "|"]
    for f in faults:
        n = sum(r["fault_rate"] == f for r in rows)
        ok = sum(r["fault_rate"] == f and r["success"] for r in rows)
        lines.append(f"| {f:.0%} | {n} | {ok} | " + " | ".join(str(counts[(f, k)]) for k in CATEGORIES) + " |")
    lines += ["", *(f"- `{k}`: {v}" for k, v in CATEGORIES.items())]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs-dir", type=Path, default=Path("runs"))
    print(audit(p.parse_args().runs_dir))


if __name__ == "__main__":
    main()
