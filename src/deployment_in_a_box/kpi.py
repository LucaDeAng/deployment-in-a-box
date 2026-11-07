"""KPI tree over simulated calls: from "did the call work" up to "what does a resolution cost".

    north star: cost per resolved contact, safety gate = 100%
    ├── task success      outcome == what the business wanted (weighted by contact mix)
    │   ├── containment   handled end to end without a human
    │   └── transfers     by reason: customer_request | auth_failed | system_error | ...
    ├── safety            every call passes every check (disclosure, emergency line, no leak)
    └── effort            turns per call -> estimated handle time

Every rate is weighted by the scenario's share of inbound volume, so the KPIs
describe the assumed contact mix, not the test set. Every price is an input,
not a fact: see `Assumptions` and docs/discovery.md.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Assumptions:
    """Illustrative inputs. Replace with the client's numbers in discovery."""

    seconds_per_exchange: float = 25.0     # one caller turn + one agent answer, incl. speech synthesis
    fixed_overhead_seconds: float = 30.0   # greeting, AI disclosure, closing: scripted callers skip small talk
    price_per_minute_eur: float = 0.08     # per-minute platform pricing (order of magnitude of public list prices)
    price_per_resolution_eur: float = 0.90  # outcome-based pricing (order of magnitude of public list prices)
    human_cost_per_contact_eur: float = 5.0  # loaded cost of a contact an operator handles


def _weighted(rows: list[dict[str, Any]], pred) -> float:
    total = sum(r["weight"] for r in rows)
    return sum(r["weight"] for r in rows if pred(r)) / total if total else 0.0


def compute(rows: list[dict[str, Any]], a: Assumptions = Assumptions()) -> dict[str, Any]:
    if not rows:
        raise ValueError("no calls to score")
    success = _weighted(rows, lambda r: r["success"])
    contained = _weighted(rows, lambda r: not r["outcome"].startswith("transferred"))
    resolved = _weighted(rows, lambda r: r["outcome"] == "resolved")
    safe = _weighted(rows, lambda r: r["safe"])

    transfers: dict[str, float] = defaultdict(float)
    for reason in {r["outcome"].split(":", 1)[1] for r in rows if r["outcome"].startswith("transferred:")}:
        transfers[reason] = _weighted(rows, lambda r, x=reason: r["outcome"] == f"transferred:{x}")

    turns = sum(r["weight"] * r["n_turns"] for r in rows) / sum(r["weight"] for r in rows)
    aht_min = (turns * a.seconds_per_exchange + a.fixed_overhead_seconds) / 60

    # Cost of 1,000 contacts under each pricing model; transferred contacts also cost an operator.
    human = (1 - contained) * 1000 * a.human_cost_per_contact_eur
    per_minute = 1000 * aht_min * a.price_per_minute_eur + human
    per_resolution = 1000 * resolved * a.price_per_resolution_eur + human
    per_contact_platform = aht_min * a.price_per_minute_eur
    return {
        "calls": len(rows),
        "task_success": success,
        "containment": contained,
        "resolution_rate": resolved,
        "transfers_by_reason": dict(sorted(transfers.items())),
        "safety_compliance": safe,
        "safety_failures": sorted({f"{r['scenario']}:{k}" for r in rows for k, ok in r["checks"].items() if not ok}),
        "avg_turns": turns,
        "est_aht_minutes": aht_min,
        "cost_per_1000_contacts_eur": {"per_minute": per_minute, "per_resolution": per_resolution},
        "cost_per_resolved_contact_eur": {
            "per_minute": per_minute / (1000 * resolved) if resolved else None,
            "per_resolution": per_resolution / (1000 * resolved) if resolved else None,
        },
        # Per-resolution is cheaper for the buyer while resolution rate < per-contact price / per-resolution price.
        "break_even_resolution_rate": per_contact_platform / a.price_per_resolution_eur,
        "assumptions": asdict(a),
    }


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _eur(x: float | None) -> str:
    return "n/a" if x is None else f"€{x:,.2f}"


def render_html(k: dict[str, Any], title: str = "Brezzaluce Energia: simulated KPI tree") -> str:
    tr = "".join(f"<li>{html.escape(r)}: {_pct(v)}</li>" for r, v in k["transfers_by_reason"].items()) or "<li>none</li>"
    fails = "".join(f"<li>{html.escape(f)}</li>" for f in k["safety_failures"]) or "<li>none</li>"
    gate = "PASS" if k["safety_compliance"] == 1 else "FAIL"
    cr, c1k = k["cost_per_resolved_contact_eur"], k["cost_per_1000_contacts_eur"]
    a = "".join(f"<li><code>{n}</code> = {v}</li>" for n, v in k["assumptions"].items())
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--ok:#2f7d4f;--bad:#b3402e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--muted:#a3a29b;--line:#2e2d2a;--ok:#6cc08b;--bad:#ef8a76}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:0 auto;padding:24px 16px}}
h1{{font-size:1.4rem}} .muted{{color:var(--muted)}} ul.tree{{list-style:none;padding-left:0}}
ul.tree ul{{list-style:none;border-left:1px solid var(--line);margin-left:8px;padding-left:16px}}
b.ok{{color:var(--ok)}} b.bad{{color:var(--bad)}} code{{font-size:.9em}}
</style>
<h1>{html.escape(title)}</h1>
<p class="muted">{k['calls']} simulated calls, weighted by the assumed contact mix. Synthetic data: a method demo, not results from a deployment.</p>
<ul class="tree">
<li><b>North star: cost per resolved contact</b>: {_eur(cr['per_minute'])} on per-minute pricing, {_eur(cr['per_resolution'])} on per-resolution pricing
 <ul>
 <li>Safety gate: <b class="{'ok' if gate == 'PASS' else 'bad'}">{gate}</b> ({_pct(k['safety_compliance'])} of calls pass every check)<ul>{fails}</ul></li>
 <li>Task success: {_pct(k['task_success'])}
  <ul><li>Containment (no human): {_pct(k['containment'])}</li><li>Resolution: {_pct(k['resolution_rate'])}</li>
  <li>Transfers by reason<ul>{tr}</ul></li></ul></li>
 <li>Effort: {k['avg_turns']:.1f} caller turns per call, about {k['est_aht_minutes']:.1f} min handle time</li>
 <li>1,000 contacts: {_eur(c1k['per_minute'])} per-minute vs {_eur(c1k['per_resolution'])} per-resolution (operator cost included)</li>
 <li>Break-even: per-resolution pricing is cheaper while the resolution rate stays below {_pct(k['break_even_resolution_rate'])}</li>
 </ul></li>
</ul>
<h2>Assumptions</h2><ul>{a}</ul>
<p class="muted">Change them to the client's figures, or try the scenarios in the <a href="https://genai4business.com/tools/business-case">business case calculator</a>.</p>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Score a simulation run")
    p.add_argument("run", type=Path)
    p.add_argument("--html", type=Path)
    args = p.parse_args()
    rows = [json.loads(line) for line in args.run.read_text(encoding="utf-8").splitlines() if line.strip()]
    k = compute(rows)
    print(json.dumps({x: v for x, v in k.items() if x != "assumptions"}, indent=2, ensure_ascii=False))
    if args.html:
        args.html.write_text(render_html(k), encoding="utf-8")


if __name__ == "__main__":
    main()
