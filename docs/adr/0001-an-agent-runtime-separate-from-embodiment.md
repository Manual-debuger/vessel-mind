# ADR 0001 — An agent runtime, separate from embodiment

- **Status:** Accepted · amended 2026-10-26 (see [Amendments](#amendments))
- **Date:** 2026-10-25
- **Related:** [0002 demo exposure](0002-demo-exposure-cloudflare-tunnel-access.md) · spec [`a-agent-track.md`](../specs/a-agent-track.md)

## Context

Vessel is a voice companion with a 3D avatar. Its build order is risk-first for a *voice*
product: measure the latency budget, make the avatar speak, and only then build the agent loop.
That order has stalled on audio and hardware work, and Vessel still has no agent loop.

The agent itself — tool use, MCP, retrieval with citations, approval of side effects, choosing a
model per task for quality and cost, evaluation — does not depend on audio or an avatar. Building
it inside Vessel ties every commit to Vessel's GPU, CUDA and speech-service checks, and buries
the agent work under a voice-first history.

## Decision

**vessel-mind is a standalone agent runtime, written in Python. Vessel will consume it over a
documented event protocol; vessel-mind never depends on Vessel.**

### 1. Python, one language

The runtime, its server and its console are Python. Retrieval, embeddings and evaluation tooling
are strongest in Python, and the runtime has no browser code that would need TypeScript. Vessel
stays TypeScript; the boundary between them is a wire protocol (§6), not a shared library, so
the two languages never meet in one build.

### 2. Own the loop, over a provider-neutral seam

The loop is written here, not taken from a framework, and talks only to a `ChatModel` interface:
messages and tool definitions in; an async stream of `text`, `tool_call` and `stop` events out;
cancellable. One adapter ships first:

- `openai-compatible` — `/v1/chat/completions` with `tools`, called with `httpx`; served by
  `llama-server` and by most hosted and local runtimes.

An `anthropic` adapter (the Messages API through the `anthropic` SDK) is deferred; see
Amendments. The types behind the seam are this project's own, not one provider's wire format, so
adding an adapter never changes the loop. A provider SDK is an adapter, not a dependency of the
loop. **A local model can answer any turn the
router sends it.**

### 3. Route by what the turn needs, not by where the model lives

| Path | Model | Handles |
|---|---|---|
| **Fast** | a small model — a local quantised GGUF on `llama-server` today | turn classification; answers where retrieval already supplied the context |
| **Slow** | a frontier-class hosted model — a free-tier provider today | multi-step tool use; anything needing real reasoning |

Neither path names a location. **The app must start with both paths pointed at hosted models**;
if it cannot run without local weights, the seam is fake. How much a local model can carry is not
argued here — the eval harness measures it per configuration (hosted, local, routed).

### 4. The embodiment contract

So that Vessel can drive voice from this loop later without changing it, every `ChatModel`
adapter and the loop itself must provide:

- **token-level streaming** — speech starts on the first sentence;
- **cancellation mid-generation** — barge-in;
- **tool-call events** — the voice can say "let me check" while a tool runs.

### 5. Side effects need a human

Every tool carries capability labels (resource, effect, exfiltration, taint); a tool without
labels is not registered. Tools whose effect is `write` or `irreversible` are **never executed by
the loop**: the loop produces a proposal, and only an explicit approval executes it, once.

### 6. The protocol is the product boundary; the console is for operators

The runtime's public surface is an **event protocol**: a turn is started over HTTP and its
events (`text`, `tool_start`, `tool_result`, `proposal`, `route`, `done`, `error`) stream back as
Server-Sent Events. The event models are pydantic, and their **JSON Schema is exported and
committed**, so Vessel generates its TypeScript types from it instead of copying them by hand.

The browser page in this repository is an **operator console**, not the product UI: it shows
what a user interface would hide — every tool call with its input and result, proposals, the
routing decision, citations, token usage. It is server-rendered (Jinja + htmx) with no front-end
build. The end-user experience is Vessel's job.

### 7. Public corpus only

This repository will be public, and every file is written as if it already were — it may start
private, and publishing exposes the whole history, not only the latest commit. **No personal data
enters version control, the eval set, fixtures, results or screenshots.** The demo and golden set
use public documentation only. A personal workspace (for example Notion) may be connected at
runtime through configuration; it is never committed and never used for reported numbers.

## Consequences

- Vessel's voice track resumes later as a client of this protocol; its own agent-loop plans are
  superseded by this repository.
- Vessel's capability-label and credential rules are carried over as pydantic models with the
  same semantics; Vessel's TypeScript versions remain for Vessel's own client-side use.
- Integrating a third-party backend (for example OpenClaw) behind the same seam, and running the
  loop on the user's PC rather than the server, are deferred, not rejected.
- Memory consolidation, an MCP server of our own, and hosted tracing are out of the first pass;
  telemetry is JSONL.
- Known gaps for the companion client (recorded 2026-10-26; no interfaces are added for them in
  the first pass):
  1. **Barge-in history.** After an interruption, history should keep only what was spoken; the
     cancel request will need an optional "heard up to here" field (an additive change).
  2. **Memory writes.** §5 requires approval for every write; a companion writes its own memory
     constantly. Telling writes to the agent's own state apart from writes to the outside world
     needs a future ADR.
  3. **Router latency.** The §3 router is serial and chosen for quality and cost: its call
     precedes the first token. Voice will likely need a parallel *handoff* mode — the fast model
     speaks first and the slow model continues — which the embodiment contract already allows.
     A future ADR decides it.

## Alternatives considered

**Build it inside Vessel.** Rejected: every commit pays for Vessel's GPU and speech checks, and
the agent work is hard to find in a voice-first repository.

**TypeScript, to share code with Vessel.** Rejected: the shared surface is a protocol, which a
schema serves better than a shared package, and the target roles ask for Python.

**Python runtime plus a TypeScript React console in the same repository.** A nicer console, but
two toolchains in a one-week sprint, for a page whose audience is the operator.

**Use an agent framework (LangGraph, OpenAI Agents SDK).** Faster to a demo, but it hides the
streaming and cancellation control the embodiment contract needs, and building the loop is part
of what this project demonstrates.

## Amendments

**2026-10-26: no paid model API; the `anthropic` adapter is deferred.** The project runs with no
extra payment. A Claude subscription does not include API usage, so the app cannot call Claude
without paid credits. Both paths therefore use hosted free-tier models through the
`openai-compatible` adapter, and the slow path stays. The `anthropic` adapter is removed from the
first pass, not rejected: because the seam's types are provider-neutral, adding it later is one
adapter module plus its fixtures. Rejected options: keep the `anthropic` adapter tested only
against hand-written fixtures (code that never runs live is weak evidence), and pay for a small
capped amount of credits (the no-payment rule).
