# Voice round trip: scaffold

**Status: not built.** Nothing in this folder has run with audio, and no voice agent
or voice-platform account has been set up for this repo. This page lists the steps to
put a voice front end in front of the tools the text harness already tests, and what
to measure once it runs.

Recording: [[TODO-LUCA: 2-min recording]]

## What stays the same

The MCP server is the contract. A voice front end changes how words reach the agent,
not what the agent may do. It exposes five tools
([`mcp_server.py`](../src/deployment_in_a_box/mcp_server.py)):

| Tool | Arguments | Identity check |
|---|---|---|
| `verify_customer` | `customer_id` (C-1234), `birth_date` (YYYY-MM-DD) | returns the `session_token` |
| `get_bill` | `session_token` | token required |
| `submit_meter_reading` | `session_token`, `reading` (whole number) | token required |
| `report_outage` | `address` (street and number), `kind` (`electricity` or `gas`), `danger` | none |
| `transfer_to_human` | `reason` (`customer_request`, `auth_failed`, `out_of_policy`, `complaint`, `system_error`), `summary` | none |

Reuse the system prompt in
[`system_prompt.md`](../src/deployment_in_a_box/system_prompt.md) (rule 10 is already
written for speech: two sentences at most, amounts with a comma, dates in words) and the
greeting in [`simulate.py`](../src/deployment_in_a_box/simulate.py), which carries the
AI disclosure.

## Step 0: serve the tools over HTTP

```bash
uv run python -m deployment_in_a_box.mcp_server --http --port 8000
# in another terminal, any tunnel, for example:
cloudflared tunnel --url http://127.0.0.1:8000        # or: ngrok http 8000
# restart the server with the tunnel's hostname, or it answers 421 to the tunnel:
uv run python -m deployment_in_a_box.mcp_server --http --port 8000 --allowed-host <tunnel-host>
```

The endpoint is `https://<tunnel-host>/mcp` (streamable HTTP). Before exposing it:

- **The server has no authentication.** The back office is simulated and the data is
  synthetic, so the risk is low, but keep the tunnel up only while testing, or put a
  shared secret in front of it.
- **One back office for every session.** Three wrong birth dates lock C-2077 until the
  server restarts, and readings and tickets pile up. Restart between test runs.
- **Faults are off by default.** Set `BREZZALUCE_FAULT_RATE=0.2` to test the voice agent
  against a failing back office, as the text eval does.

## Option A: ElevenLabs Agents

Quickest to stand up: the platform handles turn-taking, speech recognition and speech
synthesis, and calls the tools over MCP.

1. Create an agent in the ElevenLabs dashboard (by hand; this repo creates nothing).
2. Language Italian. Pick a stock Italian voice.
3. System prompt: paste `system_prompt.md`. First message: the greeting from `simulate.py`.
4. LLM: pick Claude if the agent settings offer it, so voice results compare with the
   text eval. Note the exact model in the results.
5. Tools: add an MCP server with the tunnel URL above. The alternative is server tools
   (one webhook per tool), which needs a small REST wrapper around the same five
   functions. The MCP route avoids writing that wrapper.
6. Test from the dashboard's test call first, then a phone number if needed.

What you give up: less visibility into per-stage latency, and the tool loop is the
platform's, not `ClaudeAgent`'s, so a voice failure may come from a different loop than
the one the text eval scored.

## Option B: Pipecat

More work, and more control: the same agent code as the text eval.

```
caller audio ─▶ transport (WebRTC or telephony) ─▶ VAD ─▶ STT (Italian) ─▶ Claude + the five MCP tools ─▶ TTS (Italian) ─▶ caller
```

1. Transport: WebRTC for desk tests, a telephony WebSocket for phone calls.
2. VAD with interruptions on, so barge-in cuts the agent's audio.
3. STT with an Italian model. Log the transcript of every caller turn.
4. LLM step: either Pipecat's Anthropic service with the MCP tools registered through
   its MCP client (check the current Pipecat docs for the names), or a custom processor
   that calls `ClaudeAgent.turn(text, tools)` from `agents.py`. The second keeps the
   exact loop the text eval scored.
5. TTS with an Italian voice.
6. Write each call in the simulation's JSONL trace format (`turns` with `tool_calls`), so
   `simulate.judge` and the KPI tree score voice calls with the same safety checks.

## What to measure

The text harness cannot see these. Each is a reason a call that passes in text fails
on the phone.

- **Spoken entities.** Compare the argument the agent passed to the tool with the
  scenario's fact, per entity type:
  - customer code "C-1042": letter plus digits, said as "ci millequarantadue" or
    "ci uno zero quattro due";
  - dates of birth: "12 aprile 1985" and "dodici quattro ottantacinque" both have to
    become `1985-04-12`;
  - meter reading "45231" against "452310": the `meter-typo` scenario only works if the
    digit count survives speech recognition;
  - amounts read back: "84,30 euro".

  Spoken-entity results for Italian ASR are in
  [spoken-entities-it](https://github.com/LucaDeAng/spoken-entities-it).
- **Latency.** p50 and p95 from the end of the caller's speech to the first agent audio,
  split into STT, LLM (with tool calls) and TTS. Report turns with a tool call apart from
  turns without one.
- **Barge-in.** The caller talks over the agent. Does the audio stop? In the gas
  scenario, does "800 000 999" still come first when the caller interrupts the safety
  message? That is the `emergency_first` check under interruption.
- **The same 11 scenarios**, spoken by a person or a TTS caller, scored by the same judge.
  Then DTMF for the birth date, which removes one spoken entity from the riskiest step.
