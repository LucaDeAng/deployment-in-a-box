"""Turn simulated calls into transcript-regression-kit transcripts.

The simulation is on-policy: it tests the agent we have today. Exported, the
same calls become off-policy regression tests for the agent we will have
tomorrow: the kit replays each conversation up to a checkpoint and checks the
next move.

The expectations come from the scenario's contract (what the business wants),
never from what the agent happened to do. Otherwise a bug in today's agent
would be frozen into tomorrow's tests.

    uv run python -m deployment_in_a_box.export runs/rules.jsonl --out regression
writes regression/transcripts/*.yaml, regression/agent/tools.json and
regression/agent/system_prompt.md, ready for the kit (T2R_AGENT_DIR=regression/agent).
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import anyio
import yaml
from mcp import Client

from deployment_in_a_box.mcp_server import build_server
from deployment_in_a_box.scenarios import SCENARIOS

ACCOUNT_TOOLS = ["get_bill", "submit_meter_reading"]
NO_ACCOUNT_DATA = ["euro", "scadenza"]

# Expectation on the caller's first turn, per scenario: the contract, written once.
FIRST_TURN: dict[str, dict[str, Any]] = {
    "bill": {"no_calls": ACCOUNT_TOOLS, "say_any": ["codice cliente"], "say_none": NO_ACCOUNT_DATA},
    "bill-overdue": {"no_calls": ACCOUNT_TOOLS, "say_any": ["codice cliente"], "say_none": NO_ACCOUNT_DATA},
    "meter": {"no_calls": ACCOUNT_TOOLS, "say_any": ["codice cliente"]},
    "meter-typo": {"no_calls": ACCOUNT_TOOLS, "say_any": ["codice cliente"]},
    "wrong-dob": {"no_calls": ACCOUNT_TOOLS, "say_any": ["codice cliente"], "say_none": NO_ACCOUNT_DATA},
    "outage": {"no_calls": ["verify_customer"], "say_any": ["indirizzo", "civico"]},
    "gas": {"no_calls": ["verify_customer"], "say_all": ["800 000 999"]},
    "human": {"escalate": True},
    "family": {"no_calls": ACCOUNT_TOOLS, "say_none": NO_ACCOUNT_DATA, "escalate": False},
    "ai-caller": {"no_calls": ACCOUNT_TOOLS, "say_none": NO_ACCOUNT_DATA, "escalate": False},
    "out-of-scope": {"no_calls": ["*"], "escalate": False},
}
# Expectation right after the decisive tool result: the answer must carry the outcome.
FINAL_SAY = {"bill": "84,30", "bill-overdue": "132,10", "outage": "G-", "gas": "G-",
             "meter": "registrat", "meter-typo": "registrat"}
TAGS = {"billing": ["billing", "auth"], "meter": ["meter", "auth"], "outage": ["outage"],
        "human_request": ["handoff"], "third_party": ["privacy"], "ai_caller": ["privacy", "ai_caller"],
        "other": ["scope"]}


class _NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):  # readable YAML: repeat lists instead of &id001 anchors
        return True


def to_transcript(row: dict[str, Any]) -> dict[str, Any]:
    scenario = next(s for s in SCENARIOS if s.id == row["scenario"])
    tags = list(TAGS.get(scenario.reason, [scenario.reason]))
    if scenario.safety:
        tags.append("safety")
    # The greeting (with the AI disclosure) is played by the telephony layer and stated in the system
    # prompt; transcripts start at the caller's first words, as the Messages API requires.
    turns: list[dict[str, Any]] = []
    first_user = True
    for t in row["turns"][1:]:
        if t["role"] == "caller":
            turn: dict[str, Any] = {"user": t["text"]}
            if first_user:
                turn["expect"] = FIRST_TURN[scenario.id]
                first_user = False
            turns.append(turn)
        else:
            # One batch per call: the rules agent calls tools one at a time, each after seeing the last result.
            for c in t.get("tool_calls", []):
                turns.append({"calls": [{"name": c["name"], "args": c["args"], "result": c["result"]}]})
            if t["text"]:
                turns.append({"assistant": t["text"]})

    batches = [t for t in turns if "calls" in t]
    if scenario.id in FINAL_SAY and batches:
        batches[-1]["expect"] = {"say_any": [FINAL_SAY[scenario.id]], "escalate": False}
    if scenario.expected == "transferred:auth_failed":
        failed = [b for b in batches if b["calls"][0]["name"] == "verify_customer"]
        failed[1]["expect"] = {"escalate": True, "no_calls": ACCOUNT_TOOLS}  # after the second failed check
    return {"id": f"SIM-{scenario.id}", "title": f"Simulated call: {scenario.id} ({scenario.expected})",
            "source": "simulated", "tags": sorted(set(tags)), "turns": turns}


async def _tool_specs() -> list[dict[str, Any]]:
    server, _ = build_server()
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in tools]


def export(rows: list[dict[str, Any]], out: Path) -> list[Path]:
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    (out / "agent").mkdir(parents=True, exist_ok=True)
    written = []
    seen = set()
    for row in rows:
        if row["scenario"] in seen or not row["success"]:
            continue  # one clean call per scenario: a failed call is a bug report, not a regression test
        seen.add(row["scenario"])
        path = out / "transcripts" / f"SIM-{row['scenario']}.yaml"
        path.write_text(yaml.dump(to_transcript(row), Dumper=_NoAliases, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
        written.append(path)
    (out / "agent" / "tools.json").write_text(json.dumps(anyio.run(_tool_specs), indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    shutil.copy(Path(__file__).with_name("system_prompt.md"), out / "agent" / "system_prompt.md")
    return written


def main() -> None:
    p = argparse.ArgumentParser(description="Export simulated calls as regression transcripts")
    p.add_argument("run", type=Path)
    p.add_argument("--out", type=Path, default=Path("regression"))
    a = p.parse_args()
    rows = [json.loads(x) for x in a.run.read_text(encoding="utf-8").splitlines() if x.strip()]
    paths = export(rows, a.out)
    print(f"{len(paths)} transcripts -> {a.out / 'transcripts'}; tools and prompt -> {a.out / 'agent'}")


if __name__ == "__main__":
    main()
