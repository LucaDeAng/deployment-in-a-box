# Deployment in a box

[![ci](https://github.com/LucaDeAng/deployment-in-a-box/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaDeAng/deployment-in-a-box/actions/workflows/ci.yml)

A voice agent for **Brezzaluce Energia**, a fictional Italian electricity and gas
supplier, deployed the way a forward deployed engineer would deploy it. The agent
is the easy part. This repo is about what surrounds it:

1. **Discovery.** Who calls, why, and what "good" means, written down before any
   code: [`docs/discovery.md`](docs/discovery.md).
2. **An integration layer the agent cannot talk its way around.** An MCP server
   in front of a simulated back office. Identity is a signed, expiring token,
   the IBAN is masked at the source, and errors carry a `retryable` flag.
3. **On-policy simulation.** Eleven scripted callers, weighted by the assumed
   contact mix, talk to the agent through the real MCP tools, with backend
   faults injected at 0%, 20% and 50%.
4. **A KPI tree with a safety gate.** From "did the call work" up to "what does
   a resolution cost", on per-minute and per-resolution pricing. CI fails on a
   single safety violation.
5. **Regression tests for tomorrow.** Every clean simulated call is exported as
   a transcript for the [transcript-regression-kit](https://github.com/LucaDeAng/transcript-regression-kit),
   so the next prompt or model change is replayed against it.

Everything here is **synthetic**: the company, customers, prices and traffic
mix. The numbers below show the method working, not the results of a deployment.

## Run it

```bash
uv sync
uv run pytest -q                                   # 36 tests: server, simulation, KPIs, export
uv run python -m deployment_in_a_box.simulate --fault-rate 0.2 --seeds 5 --out runs/faults.jsonl
uv run python -m deployment_in_a_box.kpi runs/faults.jsonl --html runs/report.html --min-safety 1
uv run python -m deployment_in_a_box.export runs/faults.jsonl --out regression
uv run python -m deployment_in_a_box.mcp_server    # the MCP server over stdio, for any MCP client
```

`--agent claude` swaps the deterministic rules agent for Claude (needs
`ANTHROPIC_API_KEY`). Claude gets its tool definitions from the MCP server's own
tool list, so the contract has one source of truth.

## Architecture

```
scripted caller ──text──▶ agent (rules | Claude) ──MCP tools──▶ integration layer ──▶ simulated back office
      ▲                         │                                 token · masking ·        customers, bills,
      └──────── reply ──────────┘                                 validation · audit       outages, faults
                                │
                     JSONL trace per call ──▶ judge (outcome + safety checks) ──▶ KPI tree ──▶ HTML report, CI gate
                                └──────────▶ export ──▶ regression-kit transcripts
```

| Tool | What it enforces |
|---|---|
| `verify_customer` | Same response shape for unknown and wrong customers, so callers cannot probe which accounts exist. Three failures lock the account. |
| `get_bill` | Takes only a session token; the customer comes from the token. Returns `iban_last4`, never the IBAN. |
| `submit_meter_reading` | Rejects readings below the last one or implausibly high, telling the agent to ask again. |
| `report_outage` | Requires a street number. Gas with danger is P1 and carries the emergency line. |
| `transfer_to_human` | Requires a real summary, so the caller does not repeat themselves. `system_error` is its own reason. |

## What the simulation shows (rules agent, 55 calls per run)

| Injected backend faults | Task success | Containment | Safety | Transfers |
|---|---|---|---|---|
| 0% | 100% | 85% | 100% | customer request 10%, auth failed 5% |
| 20% | 97% | 82% | 100% | + system error 3% |
| 50% | 59% | 46% | 100% | + system error 41% |

Rates are weighted by the contact mix. Under faults the agent degrades into
handoffs that state the reason, never into unsafe calls. With no faults and the assumptions in
`docs/discovery.md`, cost per resolved contact is about €1.20 on per-minute
pricing and €1.90 on per-resolution pricing. It is driven by the operator cost of
the 15% of calls that reach a human, not by the platform price, so **containment
is the lever, not the pricing model**. Per-resolution pricing only wins below
about an 11% resolution rate. A sample report is in
[`docs/kpi-report.html`](docs/kpi-report.html).

## What the simulation caught before any user did

Each of these was found by running the simulation, then fixed and pinned by a test:

- **"Vorrei cambiare operatore telefonico"** (I want to change phone carrier)
  triggered a handoff to a human operator. That was a keyword false positive.
- **A backend timeout during verification told the caller "your details don't
  match"** and counted as a failed identity check. It now hands off with
  `system_error` and never blames the caller.
- **A gas leak with the system down reached the operator without the address.**
  The handoff summary now carries it.
- **An outage ticket could be logged as "45, Napoli"** with no street. The judge
  now checks that tickets are actionable.

## Limits, and what I would do next

- **Text, not audio.** The simulation skips speech recognition errors,
  barge-in, silence and background noise. Those are the next layer, and the
  failure modes current voice benchmarks focus on.
- **Scripted callers are narrow.** They answer only what is asked, which is good
  for catching missing questions but cannot improvise. An LLM-simulated caller,
  with its own rubric, would widen coverage.
- **The rules agent is a baseline.** It makes CI reproducible without an API
  key. The live Claude agent has the same interface and has not been run here yet.
- **Auth is a demo token.** The MCP authorization spec now expects OAuth 2.1
  with audience-bound tokens. In production the session token would be one,
  issued after the identity check.
- **AI callers are refused.** Consumer agents already call businesses. A
  declared agent with a scoped, delegated credential is the open design question,
  and the subject of the next asset.

MIT licence. Brezzaluce Energia, its customers and the emergency number
800 000 999 are fictional.
