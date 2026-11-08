# Deployment in a box

[![ci](https://github.com/LucaDeAng/deployment-in-a-box/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaDeAng/deployment-in-a-box/actions/workflows/ci.yml)

**A deployment harness for a phone agent, tested in text.** The customer is
Brezzaluce Energia, a fictional Italian electricity and gas supplier. The harness
covers what decides whether an agent survives production: discovery, an
integration layer the agent cannot talk its way around, simulated callers, a KPI
tree with a safety gate, and regression tests for the next change.

| | |
|---|---|
| **6 bugs** | found before any user: 4 in the agent by the simulation, 2 in the judge by review (below) |
| **0 safety failures** | at 0%, 20% and 50% injected backend faults. Every check has a negative-control test proving it can fail. |
| **18 checkpoints** | exported from 11 simulated conversations as regression tests for the [transcript-regression-kit](https://github.com/LucaDeAng/transcript-regression-kit) |

Everything is **synthetic**: the company, customers, prices and traffic mix.
These numbers describe the method, not the results of a deployment.

## What the harness caught

Each problem was found by running it, then fixed and pinned by a test.

- **"Vorrei cambiare operatore telefonico"** (I want to change phone carrier)
  triggered a handoff to a human operator. That was a keyword false positive.
- **A backend timeout during verification told the caller "your details don't
  match"** and counted as a failed identity check. It now hands off with its own
  reason, `system_error`, and never blames the caller.
- **A gas leak with the system down reached the operator without the address.**
  The handoff summary now carries it.
- **An outage ticket could be logged as "45, Napoli"** with no street. The back
  office now rejects it, and the judge checks that every ticket is actionable.
- **"Refused" could not fail.** An agent that asked a relative for the holder's
  birth date still scored as a correct refusal. A refusal now has to say why
  (holder only, or out of scope) and must not start an identity check.
- **The judge itself leaked.** A review injected an agent that never verified
  the caller and read out an amount and a full IBAN, and it still scored
  "resolved, safe": the leak check only ran on refusal scenarios, and outcomes
  were judged on words. Outcomes now come from the back office's end state, the
  way τ-bench judges them. Leak checks run on every call.

## How it fits together

```
scripted caller ──text──▶ agent (rules | Claude) ──MCP tools──▶ integration layer ──▶ simulated back office
      ▲                         │                                 token · masking ·        customers, bills,
      └──────── reply ──────────┘                                 validation · audit       outages, faults
                                │
                     JSONL trace per call ──▶ judge (end state + safety checks) ──▶ KPI tree ──▶ report, CI gate
                                └──────────▶ export ──▶ regression-kit transcripts
```

1. **Discovery first.** Contact mix, KPI definitions, pricing assumptions and a
   rollout plan: [`docs/discovery.md`](docs/discovery.md).
2. **MCP integration layer.**
   - `verify_customer` returns a signed, expiring session token, and account tools
     derive the customer from that token.
   - The IBAN is masked at the source.
   - Unknown and wrong customers get the same response, so callers cannot
     probe which accounts exist.
   - Errors carry a `retryable` flag, and faults are injectable.
3. **On-policy simulation.** Eleven scripted callers, weighted by the assumed
   contact mix, answer only what the agent asks. If the agent forgets to ask for
   the street number, the caller never volunteers it.
4. **KPI tree and gate.**
   - It goes from task success and transfers by reason up to the cost per
     AI-resolved contact.
   - CI fails on a single safety violation.
   - A sample report is in [`docs/kpi-report.html`](docs/kpi-report.html).
5. **Regression export.** Expectations come from each scenario's contract,
   never from what the agent did. Otherwise today's bugs would become
   tomorrow's "expected behaviour".

## Run it

```bash
uv sync
uv run pytest -q                                   # 44 tests: server, simulation, judge controls, KPIs, export
uv run python -m deployment_in_a_box.simulate --fault-rate 0.2 --seeds 5 --out runs/faults.jsonl
uv run python -m deployment_in_a_box.kpi runs/faults.jsonl --html runs/report.html --min-safety 1
uv run python -m deployment_in_a_box.simulate --out runs/clean.jsonl   # regression/ is exported from the clean run
uv run python -m deployment_in_a_box.export runs/clean.jsonl --out regression
uv run python -m deployment_in_a_box.mcp_server    # the MCP server over stdio, for any MCP client
```

`--agent claude` swaps the rules agent for Claude, which gets its tool
definitions from the MCP server's own tool list. It needs `ANTHROPIC_API_KEY`.
Expect stricter scoring than for the rules agent: the scripted callers and some
checks react to exact phrases (for example "800 000 999"), so a correct
paraphrase can fail. That makes results look worse, never better.

To replay the exported transcripts against a candidate agent with the kit:

```bash
cd ../transcript-regression-kit
T2R_AGENT=claude T2R_AGENT_DIR="$PWD/../deployment-in-a-box/regression/agent" \
  python -m t2r.run --agent claude --transcripts ../deployment-in-a-box/regression/transcripts
```

## Reading the simulation numbers honestly

The included agent is a deterministic **rules agent**. It is a check on the
harness, not a claim about agent quality, and it makes CI reproducible without
an API key.

| Injected backend faults | Distinct conversations (of 55 calls) | Task success | Containment | Safety |
|---|---|---|---|---|
| 0% | 11 | 100% | 85% | 100% |
| 20% | 22 | 97% | 82% | 100% |
| 50% | 24 | 59% | 46% | 100% |

- **The 0% row restates the inputs.** A deterministic agent gives each scenario
  one conversation, five times over. Its 85% containment is simply 1 − the
  assumed shares of `human` (10%) and `wrong-dob` (5%).
- **The fault rows are the finding.** As the back office fails, the agent
  degrades into handoffs labelled `system_error`, which reach 41% of traffic at
  50% faults, and never into an unsafe call.
- **pass^k only means something for the LLM agent.** For a deterministic agent
  it equals pass@1.

**Pricing.** Per-minute or per-resolution? It depends on handle time and on the
all-in per-minute price. At 3 minutes and €0.15/min all-in, per-resolution
pricing wins below a 50% resolution rate. At the simulated 1.3 minutes and a
voice-only €0.08/min, it wins only below 11%. The sensitivity grid and every
assumption are in [`docs/discovery.md`](docs/discovery.md).

## Limits, and what I would do next

- **The live agent has not run yet.** Next: Claude on the same 11 scenarios ×
  k = 5 trials, reporting pass^1, pass^k, results per scenario and cost per call.
- **Text, not audio.** The simulation skips speech-recognition errors, barge-in,
  silence and noise, which are the failure modes current voice benchmarks focus
  on. A cheap first step is a TTS→ASR round trip on "C-1042", birth dates, and
  "45231" against "452310" said aloud. Then DTMF for the birth date, and p50/p95
  latency.
- **Scripted callers are narrow.** They answer only what is asked, which catches
  missing questions but cannot improvise. LLM-simulated callers with personas
  would widen coverage, with the scripted ones kept as a deterministic floor.
- **Auth is a demo token.** The MCP authorization spec expects OAuth 2.1 with
  audience-bound tokens. In production the session token would be one, issued
  after the identity check.
- **AI callers are refused.** Consumer agents already call businesses, and this
  refusal names the secret to replay (the birth date). The follow-up,
  [ai-to-ai-call-lab](https://github.com/LucaDeAng/ai-to-ai-call-lab), builds on
  this repo: a declared agent presents a scoped, delegated credential, writes need
  the holder, and a looping agent gets hung up on.

MIT licence. Brezzaluce Energia, its customers and the emergency number
800 000 999 are fictional.
