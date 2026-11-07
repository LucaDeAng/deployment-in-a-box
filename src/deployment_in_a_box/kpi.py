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
    """Illustrative inputs. Replace with the client's numbers in discovery (docs/discovery.md)."""

    seconds_per_exchange: float = 25.0      # one caller turn + one spoken answer
    fixed_overhead_seconds: float = 30.0    # greeting, AI disclosure, closing
    # Cost uses an assumed handle time: scripted callers are terse, so simulated AHT is a lower bound.
    aht_minutes_for_cost: float | None = 3.0
    price_per_minute_eur: float = 0.15      # all-in: voice platform + LLM + telephony
    price_per_resolution_eur: float = 0.90  # order of magnitude of public (chat) outcome prices
    human_cost_per_contact_eur: float = 5.0  # loaded cost of a contact an operator handles
    callback_rate_refused: float = 0.5      # refused / out-of-scope callers who call back within 7 days


SENSITIVITY_AHT = (None, 3.0, 5.0)  # None = the simulated AHT
SENSITIVITY_PRICE = (0.08, 0.15, 0.25)


def _weighted(rows: list[dict[str, Any]], pred) -> float:
    total = sum(r["weight"] for r in rows)
    return sum(r["weight"] for r in rows if pred(r)) / total if total else 0.0


def compute(rows: list[dict[str, Any]], a: Assumptions = Assumptions()) -> dict[str, Any]:
    if not rows:
        raise ValueError("no calls to score")
    success = _weighted(rows, lambda r: r["success"])
    contained = _weighted(rows, lambda r: not r["outcome"].startswith("transferred"))
    resolved = _weighted(rows, lambda r: r["outcome"] == "resolved")
    refused = _weighted(rows, lambda r: r["outcome"] == "refused")
    safe = _weighted(rows, lambda r: r["safe"])

    transfers: dict[str, float] = defaultdict(float)
    for reason in {r["outcome"].split(":", 1)[1] for r in rows if r["outcome"].startswith("transferred:")}:
        transfers[reason] = _weighted(rows, lambda r, x=reason: r["outcome"] == f"transferred:{x}")

    turns = sum(r["weight"] * r["n_turns"] for r in rows) / sum(r["weight"] for r in rows)
    sim_aht = (turns * a.seconds_per_exchange + a.fixed_overhead_seconds) / 60
    aht = a.aht_minutes_for_cost if a.aht_minutes_for_cost is not None else sim_aht

    # 1,000 contacts under each pricing model; contacts that reach a human also cost an operator.
    human = (1 - contained) * 1000 * a.human_cost_per_contact_eur
    per_minute = 1000 * aht * a.price_per_minute_eur + human
    per_resolution = 1000 * resolved * a.price_per_resolution_eur + human

    def break_even(minutes: float, price: float) -> float:
        # Per-resolution is cheaper for the buyer while resolution rate < per-contact AI cost / per-resolution price.
        return minutes * price / a.price_per_resolution_eur

    return {
        "calls": len(rows),
        "unique_conversations": len({json.dumps(r["turns"], sort_keys=True) for r in rows}),
        "fault_rates": sorted({r.get("fault_rate", 0.0) for r in rows}),
        "task_success": success,
        "containment": contained,
        # a refused caller often calls back: containment net of that assumption
        "containment_net_of_callbacks": contained - refused * a.callback_rate_refused,
        "resolution_rate": resolved,
        "transfers_by_reason": dict(sorted(transfers.items())),
        "safety_compliance": safe,
        "safety_failures": sorted({f"{r['scenario']}:{k}" for r in rows for k, ok in r["checks"].items() if not ok}),
        "avg_turns": turns,
        "simulated_aht_minutes": sim_aht,
        "aht_minutes_for_cost": aht,
        "cost_per_1000_contacts_eur": {"per_minute": per_minute, "per_resolution": per_resolution},
        # numerator: AI cost + operator cost of transferred contacts; denominator: contacts the AI resolved
        "cost_per_ai_resolved_contact_eur": {
            "per_minute": per_minute / (1000 * resolved) if resolved else None,
            "per_resolution": per_resolution / (1000 * resolved) if resolved else None,
        },
        "break_even_resolution_rate": break_even(aht, a.price_per_minute_eur),
        "break_even_grid": {f"{'sim' if m is None else m} min @ {p}/min": break_even(sim_aht if m is None else m, p)
                            for m in SENSITIVITY_AHT for p in SENSITIVITY_PRICE},
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
    cr, c1k = k["cost_per_ai_resolved_contact_eur"], k["cost_per_1000_contacts_eur"]
    faults = ", ".join(_pct(f) for f in k["fault_rates"])
    a = "".join(f"<li><code>{n}</code> = {v}</li>" for n, v in k["assumptions"].items())
    rows: dict[str, dict[str, float]] = {}
    for key, v in k["break_even_grid"].items():
        minutes, price = key.split(" @ ")
        rows.setdefault(minutes, {})[price] = v
    prices = list(next(iter(rows.values())))
    grid = "<tr><th>AHT</th>" + "".join(f"<th>€{p}</th>" for p in prices) + "</tr>" + "".join(
        f"<tr><td>{'simulated' if m.startswith('sim') else m}</td>" + "".join(f"<td>{'always' if v[p] >= 1 else _pct(v[p])}</td>" for p in prices) + "</tr>"
        for m, v in rows.items())
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--ok:#2f7d4f;--bad:#b3402e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe6;--muted:#a3a29b;--line:#2e2d2a;--ok:#6cc08b;--bad:#ef8a76}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:0 auto;padding:24px 16px}}
h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:2rem}} .muted{{color:var(--muted)}} ul.tree{{list-style:none;padding-left:0}}
ul.tree ul{{list-style:none;border-left:1px solid var(--line);margin-left:8px;padding-left:16px}}
b.ok{{color:var(--ok)}} b.bad{{color:var(--bad)}} code{{font-size:.9em}}
table{{border-collapse:collapse}} th,td{{border:1px solid var(--line);padding:4px 10px;text-align:right}}
</style>
<h1>{html.escape(title)}</h1>
<p class="muted">{k['calls']} simulated calls ({k['unique_conversations']} distinct conversations) at {faults} injected backend faults,
weighted by the assumed contact mix. Synthetic data: a method demo, not results from a deployment.</p>
<ul class="tree">
<li><b>Cost per AI-resolved contact</b>: {_eur(cr['per_minute'])} on per-minute pricing, {_eur(cr['per_resolution'])} on per-resolution pricing
 <span class="muted">(AI cost plus operator cost of transferred contacts, divided by contacts the AI resolved)</span>
 <ul>
 <li>Safety gate: <b class="{'ok' if gate == 'PASS' else 'bad'}">{gate}</b> ({_pct(k['safety_compliance'])} of calls pass every check)<ul>{fails}</ul></li>
 <li>Task success: {_pct(k['task_success'])}
  <ul><li>Containment (no human): {_pct(k['containment'])}, {_pct(k['containment_net_of_callbacks'])} net of assumed callbacks</li>
  <li>Resolution (done in the back office and told to the caller): {_pct(k['resolution_rate'])}</li>
  <li>Transfers by reason<ul>{tr}</ul></li></ul></li>
 <li>Effort: {k['avg_turns']:.1f} caller turns per call ({k['simulated_aht_minutes']:.1f} min simulated, a lower bound); costs use {k['aht_minutes_for_cost']:.1f} min</li>
 <li>1,000 contacts: {_eur(c1k['per_minute'])} per-minute vs {_eur(c1k['per_resolution'])} per-resolution (operator cost included)</li>
 </ul></li>
</ul>
<h2>Which pricing model wins depends on handle time and the all-in per-minute price</h2>
<p>Per-resolution pricing is cheaper for the buyer while the resolution rate stays below the break-even below (at €{k['assumptions']['price_per_resolution_eur']} per resolution).
Resolution here: {_pct(k['resolution_rate'])}.</p>
<table>{grid}</table>
<h2>Assumptions</h2><ul>{a}</ul>
<p class="muted">Change them to the client's figures, or try the scenarios in the <a href="https://genai4business.com/tools/business-case">business case calculator</a>.</p>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Score a simulation run")
    p.add_argument("run", type=Path)
    p.add_argument("--html", type=Path)
    p.add_argument("--min-safety", type=float, help="fail (exit 1) if safety compliance is below this")
    p.add_argument("--min-success", type=float, help="fail (exit 1) if weighted task success is below this")
    p.add_argument("--summary", type=Path, help="append a markdown line here (e.g. $GITHUB_STEP_SUMMARY)")
    args = p.parse_args()
    rows = [json.loads(line) for line in args.run.read_text(encoding="utf-8").splitlines() if line.strip()]
    k = compute(rows)
    print(json.dumps({x: v for x, v in k.items() if x != "assumptions"}, indent=2, ensure_ascii=False))
    if args.html:
        args.html.write_text(render_html(k), encoding="utf-8")
    failures = []
    if args.min_safety is not None and k["safety_compliance"] < args.min_safety:
        failures.append(f"safety {k['safety_compliance']:.1%} < {args.min_safety:.0%}: {k['safety_failures']}")
    if args.min_success is not None and k["task_success"] < args.min_success:
        failures.append(f"task success {k['task_success']:.1%} < {args.min_success:.0%}")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(f"| {args.run.name} | {k['task_success']:.0%} | {k['containment']:.0%} | "
                    f"{k['safety_compliance']:.0%} | {', '.join(f'{r} {v:.0%}' for r, v in k['transfers_by_reason'].items())} | "
                    f"{'FAIL' if failures else 'pass'} |\n")
    if failures:
        raise SystemExit("KPI gate failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
