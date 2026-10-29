# A1–A4 — Agent track · implementation requirements

- **Status:** Ready to implement
- **Date:** 2026-10-25
- **Audience:** an AI coding agent working in this repository
- **Track exit:** *From a clean clone, `docker compose up` serves the operator console, where
  vessel-mind answers questions about its own design with citations, proposes a Notion write that
  happens only after Approve (shown on `localhost`; the public hostname refuses approvals), and the
  README reports quality, latency and cost for all-hosted,
  all-local and routed configurations.*
- **Required reading before starting:** ADR [0001](../adr/0001-an-agent-runtime-separate-from-embodiment.md)
  and [0002](../adr/0002-demo-exposure-cloudflare-tunnel-access.md).

## 0. Read this first

1. **Evidence over volume.** A number that was not measured is not reported. Fixtures are
   labelled as fixtures.
2. **Stop rather than decide an ADR-level question.** If something here contradicts an accepted
   ADR, stop and report; do not pick a side in code.
3. **The embodiment contract is non-negotiable** (ADR 0001 §4): token streaming, cancellation
   mid-generation, tool-call events — on every `ChatModel` adapter and on the loop.
4. **Public corpus only** (ADR 0001 §7). No personal data in git, fixtures, eval results or
   screenshots.

One-week sprint. Each day ends with `main` passing `task verify`.

| Day | Milestone | Done when |
|---|---|---|
| Mon 10/26 | Bootstrap (§5.0) + A1 core: `ChatModel`, two adapters, loop | §5.0–5.2 acceptance passes |
| Tue 10/27 | A1 protocol + console, A2 MCP and proposals | §5.3–5.5 acceptance passes |
| Wed–Thu 10/28–29 | A3: ingest, hybrid search, citations (Wed); reranker + retrieval ablation (Thu) | §5.6 acceptance passes |
| Fri 10/30 | A4: router, eval harness, results | §5.7–5.8 acceptance passes |
| Sat 10/31 | Compose with tunnel, README, demo, publish | §7 all checked |
| Sun 11/1 | Buffer | — |

**If Thursday slips, cut in this order:** (1) the `llama-server` reranker (keep CPU); (2) the
`local` eval row (report *not run*); (3) console polish; (4) the language split in the retrieval
table; (5) the public URL (Cloudflare Tunnel and Access; demo mode stays in code). **Never cut:**
approval tests, schema drift test, frozen golden set, `hosted` and slow-only rows, the demo video,
README numbers.

**Golden set:** drafted Wed 10/28; every question reviewed by Thu 10/29 evening;
frozen before Fri's first end-to-end run.

## 1. Context you need

- One user. Correctness, cancellation and evidence matter; throughput does not.
- Development happens in **WSL2, in the Linux filesystem** (`~/…`, never `/mnt/c` or `/mnt/d`).
- The GPU host runs `llama-server` (OpenAI-compatible) on the local network. Everything must also
  run with **both routing paths pointed at hosted models** (ADR 0001 §3).
- The product boundary is the **event protocol** (ADR 0001 §6). The console is an operator view
  of that protocol; it never gets an API the protocol does not have.
- Every tool — built-in or from MCP — goes through the capability registry. A tool without labels
  is not registered; never default labels.
- `write` and `irreversible` effects are never executed by the loop (ADR 0001 §5).
- **The corpus is defined in `eval/corpus.yaml`**: each source is a repo, a pinned commit and
  path globs — vessel-mind `docs/adr/`, `docs/specs/` and `GLOSSARY.md`; VibeConform (public,
  Apache-2.0) `README.md` and `docs/**/*.md` at `384d89e`. The eval reads each source at its
  pinned commit: VibeConform from the GitHub tarball, checked against its SHA-256 and cached in a
  git-ignored folder; vessel-mind by `git archive` of this clone, so it needs no token while the
  repository is private. The eval refuses to run when a source cannot be pinned. The app
  container ingests at startup when the index is missing, from the docs copied into the image
  plus the cached tarball; content hashing makes a restart a no-op. A corpus change is a commit
  to `corpus.yaml` plus an eval re-run.
- Vessel's repository is private and is **not** in the corpus, the README links or any
  fixture until its author makes it public; adding it then is a corpus change plus an eval
  re-run, not a code change.

## 2. Technology constraints

| Concern | Choice | Source |
|---|---|---|
| Language | Python ≥ 3.12, `uv`, src layout, package `vessel_mind` | ADR 0001 §1 |
| Repo tooling | VibeConform `prod-py/v1`: ruff (format + lint), pyright, pytest, `task verify`, lefthook, CI, agent guard | §5.0 |
| Async | `asyncio`; every stream is an async iterator; cancellation is task cancellation | ADR 0001 §4 |
| Models / validation | pydantic v2 | ADR 0001 §6 |
| Model adapter | OpenAI-compatible `/v1/chat/completions` with `tools`, `stream: true`, via `httpx` — no SDK; serves the free-tier hosted provider and `llama-server` | ADR 0001 §2 (amended) |
| Embeddings | provider-configurable: `fastembed` on CPU (default — needs no GPU and no key) or OpenAI-compatible `/v1/embeddings` (`llama-server --embeddings`) | ADR 0001 §3 |
| Vector + keyword store | stdlib `sqlite3` + `sqlite-vec` + FTS5, one file | this spec |
| Fusion | Reciprocal rank fusion, k = 60 | this spec |
| Reranking | cross-encoder, provider-configurable: `fastembed` on CPU (default), `llama-server` rerank endpoint, or `none`; multilingual model preferred | §5.6 |
| MCP | official `mcp` Python SDK, client, stdio transport | ADR 0001 |
| Notion | `@notionhq/notion-mcp-server`, version pinned by a committed `deploy/notion-mcp/package-lock.json`, installed in a Docker build stage and started with `node` over stdio — never `npx`; token from env; CI uses the fake Python MCP server and needs no Node | this spec |
| HTTP server | FastAPI + uvicorn; events as Server-Sent Events | ADR 0001 §6 |
| Console | Jinja2 templates + htmx (and its SSE extension) **vendored and pinned** under `static/`, no CDN, no front-end build | ADR 0001 §6 |
| Tests | pytest + pytest-asyncio; HTTP adapters tested with `httpx.MockTransport` / respx against recorded SSE fixtures, never live APIs | this spec |
| Module boundaries | import-linter contracts in `pyproject.toml`, run by a pytest test | §4 |
| Credentials | config names the env var; the value never appears in config, logs or telemetry | ADR 0001 |
| Ingress | `cloudflared` compose service + Cloudflare Access | ADR 0002 |

**Pin exact versions** in `pyproject.toml` and commit `uv.lock`.

**Day-one risk:** `sqlite-vec` is a loadable extension, and some Python builds disable
`sqlite3.Connection.enable_load_extension`. Verify on Monday that it loads under the `uv`-managed
interpreter in WSL2 and in the CI image. If it does not, switch to `apsw` and record it in
`DEVELOPING.md` — do not drop the vector store.

Verify on Monday that Gemini's OpenAI-compatible endpoint streams tool calls in a form the
adapter parses: arguments in fragments **or** in one chunk, and two parallel calls. Record a
fixture from the live response. If it fails, stop and report before more code depends on it.

## 3. Human-only prerequisites — do not attempt these

| Item | Why it cannot be delegated | Build against |
|---|---|---|
| GitHub repo `Manual-debuger/vessel-mind`: **private during the sprint, public before any job application** | author's account and timing | write every file as if it were already public |
| Before publishing: scan the **whole git history** for secrets (for example `gitleaks`) | publishing exposes every past commit | — |
| API key for the free-tier hosted provider; no paid key | author's account | recorded fixtures |
| Two Notion integrations, each shared with **one demo page only**: a **read-only** token ("Read content" only) for the public host, and a write token for the localhost recording | author's account | a fake MCP server in tests |
| `llama-server` with a chat GGUF (and optionally an embedding GGUF) on the GPU host | hardware and model choice | hosted chat, CPU embeddings |
| Cloudflare Tunnel token, hostname, Access policy | author's Cloudflare account | `localhost` |
| Demo recording | author's screen and voice | — |

## 4. Repository layout to create

```
pyproject.toml  uv.lock          # one project; package = vessel_mind
src/vessel_mind/
  core/
    chat.py          # ChatModel protocol, ChatMessage, ToolSpec, ChatEvent, Usage
    capabilities.py  # labels + registry (pydantic port of Vessel's capabilities.ts)
    credentials.py   # env-var-named auth schemes (pydantic port of Vessel's providers.ts)
    events.py        # the protocol: AgentEvent models (ADR 0001 §6)
    telemetry.py     # JSONL sink
  agent/
    loop.py  tools.py  proposals.py  router.py  mcp_client.py
    models/openai_compatible.py
  rag/
    chunk.py  embed.py  store.py  search.py  ingest.py
  server/
    app.py           # FastAPI: protocol endpoints + console routes; wires rag into agent
    templates/       # Jinja
    static/          # vendored htmx + sse extension, pinned
schema/events.schema.json        # exported from core/events.py, committed
tests/                           # mirrors src/; tests/fixtures/*.sse
eval/
  corpus.yaml  golden.jsonl  judge.md  run.py  results/
deploy/
  Dockerfile  compose.yaml       # app + cloudflared; llama-server stays external
config/agent.example.yaml        # providers, routing, MCP servers, label map, demo mode
Taskfile.local.yml               # dev, eval, schema:export (managed Taskfile.yml is not edited)
docs/adr/  docs/specs/
```

**Dependency direction** (import-linter contracts, enforced by `tests/test_architecture.py`,
so it runs inside the managed `task verify`):

- `core` imports nothing else in `vessel_mind`;
- `agent` and `rag` import only `core` — **`agent` does not import `rag`**: retrieval reaches
  the loop as an injected `Tool`;
- `server` may import everything; nothing imports `server` except `eval`.
- `eval` (the harness behind `task eval`) is a second composition root above `server`: it
  may import `server` for config, and nothing imports `eval` (amended 2026-10-26, ticket #13).

## 5. Deliverables

### 5.0 Bootstrap (Mon morning)

Create `pyproject.toml`, then `vibe init prod-py v1`, `vibe sync`, `vibe doctor`. VibeConform
provides ruff, pyright, pytest wiring, `Taskfile.yml`, `lefthook.yml`, CI and the agent guard.
Project-specific tasks go in `Taskfile.local.yml`, never in the managed `Taskfile.yml`.
**Time-boxed to one hour**; if `task verify` is not green by then, set the tooling up by hand and
record why in the PR. Port Vessel's capability labels and credential scheme to pydantic, with
tests that mirror Vessel's. Run the `sqlite-vec` load check (§2).

The repository starts **private** (§3). Any CI check that GitHub only offers free on public
repositories (CodeQL, Dependency Review) must be gated on
`github.event.repository.visibility == 'public'`, not deleted.

**Acceptance:** a clean clone runs `uv sync && task verify` green in WSL2 and in CI;
`test_architecture.py` fails when a forbidden import is added (prove it once, then revert).

### 5.1 `ChatModel` seam (A1)

```python
ChatEvent = Annotated[TextDelta | ToolCall | Stop, Field(discriminator="type")]  # pydantic
#   TextDelta(delta: str)
#   ToolCall(id: str, name: str, input: dict, provider_data: dict | None)
#   Stop(reason: Literal["end", "tool_use", "max_tokens", "aborted"], usage: Usage)

class ChatModel(Protocol):
    id: str
    def stream(self, req: ChatRequest) -> AsyncIterator[ChatEvent]: ...
    # ChatRequest: system, messages, tools, max_tokens
    # cancellation = cancelling the consuming task
```

The adapter emits **one** `ToolCall` per call, with fully parsed input. Fragments are keyed by
`index` when present; otherwise a new `id` starts a new call, and a fragment with neither
continues the last call (Gemini sends no `index` and each call whole). Fixtures cover both
dialects.

`provider_data` is opaque: the adapter writes it back unchanged on the next request, and session
history stores it. Gemini 3 needs this for its thought signature
(`extra_content.google.thought_signature`); a request without it can be refused with 400.

`Stop.reason` is `tool_use` whenever the step produced a tool call, whatever `finish_reason`
says (Gemini sends `stop`). Usage is the last `usage` seen, never a sum (Gemini repeats it,
cumulative, on every chunk); thinking tokens are recorded as total − prompt − completion when the
provider reports them only in `total_tokens`. Fixtures are recorded from decoded (un-gzipped)
bytes.

On HTTP 429, HTTP 503 or a first-byte timeout, the adapter retries with exponential backoff,
honouring `Retry-After` when present, up to a configured limit. Each path then has an ordered
**fallback list** of models in config (for example `gemini-3.8-flash` → `gemini-3.6-flash`): when
retries are exhausted, the next model is tried, and the `route` event names the model that
actually answered. The eval spaces its requests to stay inside free-tier limits; a question that
still fails is reported as *failed*, never dropped.

**Acceptance:** per adapter, a recorded fixture with text plus two tool calls yields the exact
event sequence; cancelling the consuming task mid-stream ends within 100 ms and closes the
underlying HTTP response (assert the mock stream was closed).

**Most likely to be done wrong:** fragment assembly — keying only on `index` merges Gemini's
parallel calls into one call with concatenated arguments, and keying only on `id` breaks the
dialects that split arguments across chunks by `index`. Test two parallel calls in each dialect.
Dropping `provider_data` breaks Gemini's second step.

### 5.2 The loop (A1)

`run_turn(model, tools, history, user_text) -> AsyncIterator[AgentEvent]` yields `text`,
`tool_start`, `tool_result`, `proposal`, `done` (summed usage, step count), `error`. At most 8
model steps per turn. Read tools execute; write/irreversible tools yield a `proposal` and return
`{"status": "awaiting_approval", "proposal_id": ...}` to the model as the tool result.

**Acceptance:** with a scripted fake `ChatModel`, a turn calling two read tools emits events in
order and ends `done`; a write tool's `execute` is never called; cancelling during a tool call
stops the turn with no further `text`.

### 5.3 Protocol (A1)

| Endpoint | Purpose |
|---|---|
| `GET /health` | readiness |
| `POST /turns` | start a turn; body `{session_id?, text}`, no `session_id` creates a session → `{session_id, turn_id}`; a new turn cancels the session's previous one |
| `GET /turns/{id}/events` | SSE stream of `AgentEvent` (JSON per event, `event:` = type) |
| `POST /turns/{id}/cancel` | cancel; the stream ends with `done` or `error`, never silently |
| `POST /proposals/{id}/approve` · `/reject` | resolve a proposal |

Sessions, their history (user text, assistant text, tool calls and results) and proposals are
stored in SQLite, in a state file separate from the retrieval index: the index can be rebuilt
from the corpus, the history cannot. On startup, every pending proposal is marked expired, and a
turn that was running is not resumed.

All request bodies are pydantic models; invalid input is a 422, never a crash.
`schema/events.schema.json` is generated from `core/events.py` by `task schema:export`, and a
test fails if the committed file differs from the generated one.

**Acceptance:** an integration test runs a turn through FastAPI's test client against the fake
model and reads the SSE stream to `done`; the schema drift test passes.

### 5.4 Operator console (A1)

`GET /` renders a Jinja page that consumes **only the protocol above** (htmx + SSE extension):
streaming text; a collapsible card per tool call (name, input, result); proposal cards with
Approve/Reject; citations linked to their source; per turn the route taken, models and token
usage; a Stop button calling cancel.

On the public host, the console shows one line: "Messages are sent to Google Gemini's free tier,
which may use them to improve its models. Do not enter personal data." The README says the same.

**Acceptance:** screenshot in the PR, taken on the public corpus only.

### 5.5 MCP and proposals (A2)

`mcp_client.py` starts configured MCP servers over stdio, lists their tools, and registers each
one **only if** `config/agent.yaml` maps it to labels. Notion search/read → `read`;
create/update → `write`. Approve executes once; Reject or a 10-minute expiry discards; proposals
are idempotent by id. In demo mode (ADR 0002, amended 2026-10-26) the loop still makes write
proposals; `POST /proposals/{id}/approve` returns 403 with error code `demo_mode`, and the
proposal stays pending until it expires or is rejected.

**Acceptance:** against a fake stdio MCP server: an unlabelled tool is not registered; approve
sent twice executes once; reject never executes; in demo mode a write proposal is shown, its approval returns 403
`demo_mode`, and the tool never executes.

### 5.6 Retrieval (A3)

Chunk markdown by heading (target 500–800 tokens, keep the heading path), embed, store vectors in
sqlite-vec and text in FTS5. Retrieval is a pipeline with each stage selectable in config:

```
candidates = vector | keyword | hybrid (RRF, k = 60)     → top 30
reranked   = rerank(query, candidates)                    → top k (default 5)
```

The reranker is a cross-encoder behind a `Reranker` protocol, provider-configurable like
embeddings: `cpu` (default — a cross-encoder through `fastembed`, no GPU, no key),
`llama-server` (its rerank endpoint, for example a `bge-reranker-v2-m3` GGUF on the GPU host), or
`none`. Prefer a **multilingual** reranker: questions may be asked in Traditional Chinese over
English documents. Confirm on Thursday that the pinned `fastembed` and `llama-server` versions
expose reranking; if one does not, ship the other and record it.

Results carry `source` (path + heading anchor), the fused score and, when reranked, the rerank
score. The server wires the pipeline into the loop as the `read` tool `search_docs`; answers cite
`[n]` mapped to `source`. Re-ingesting an unchanged file is a no-op (content hash).

**Acceptance:** unit tests prove RRF ordering on a hand-built example and that the reranker only
reorders (never adds or drops) candidates; `task eval -- --retrieval-only` produces the retrieval
ablation table in §5.8.

**Most likely to be done wrong:** reranking the whole corpus instead of the top 30, which makes
latency scale with the corpus. Test that the reranker receives at most 30 candidates.

### 5.7 Router (A4)

ADR 0001 §3. The fast model classifies the turn as JSON `{"route": "fast" | "slow"}` (validated
by pydantic). **slow**: the turn needs a tool other than one search, more than one search, or
reasoning beyond the retrieved text. **fast**: one retrieval answers it, or it needs no
knowledge. On the fast path the server retrieves first and supplies the context. A
classification timeout or invalid JSON falls back to the slow path. The decision is emitted as a
`route` event and logged with both models' usage.

Three configurations, each a fixed pair of models, selected in `config/agent.yaml`:

| Config | Fast path | Slow path | Used for |
|---|---|---|---|
| `hosted` | Gemini Flash-Lite | Gemini Flash | default: clean clone, CI smoke check |
| `local` | `llama-server`, small model | `llama-server`, large model | eval |
| `routed` | `llama-server`, small model | Gemini Flash | eval, public demo host |

Model ids are pinned in config with the date they were chosen.

**Acceptance:** both branches, the timeout fallback and the invalid-JSON fallback are exercised
with fake models.

### 5.8 Eval harness (A4)

`eval/golden.jsonl`: 30 questions over the public corpus, each with expected source paths,
2–4 key points and an `expected_route` (`fast` | `slow`). Include at least 5 questions written in
Traditional Chinese, so the multilingual claim in §5.6 is measured rather than assumed.

The 30 are 22 in English, ≥5 in Traditional Chinese and 3 unanswerable. The English and
unanswerable questions may be drafted for the author. A model drafts the Chinese ones directly
in Chinese, not translated, and the author verifies and edits each; the author reviews every
question, and each records who drafted it and that the author reviewed it. Questions come from the corpus at its pin,
before any end-to-end run. `golden.jsonl` is committed alone before the first end-to-end run; a
later change only fixes a real error, says why in the commit message, and triggers a full
re-run. Unanswerable questions have no expected sources; their correct answer says the documents
do not cover it.

`results.md` is generated, never hand-edited, and has two tables:

1. **Retrieval ablation** (`task eval -- --retrieval-only`, no LLM calls): rows `vector`,
   `keyword`, `hybrid`, `hybrid + rerank`; columns recall@5, MRR, retrieval latency p50/p95.
   Reported separately for the English and the Chinese questions.
2. **End to end** (`task eval -- --config hosted|local|routed`, plus a **slow-only** baseline
   row from `--config hosted --no-router`, where every turn takes the slow path): recall@5, MRR, answer score
   (share of key points the judge marks covered, rubric in `eval/judge.md`), citation
   correctness (checked by code: each cited source is in the expected sources),
   p50/p95 turn latency, tokens and **list-price cost**: tokens × the provider's paid list
   price, dated in config and labelled "at list price, YYYY-MM-DD"; nobody paid it. `local`
   rows show $0 with "GPU electricity not measured".
   Routed rows also report router accuracy against `expected_route`, a 2×2 confusion table, and
   the count of **costly misroutes**: fast-path answers scoring ≥ 0.25 below the slow-only
   baseline on the same question (answer score on a 0–1 scale).

The **judge** is any OpenAI-compatible model set in `config/agent.yaml` under `eval.judge`
(default the local slow model, `qwen3.6:35b-a3b` on Ollama: Gemini's free quota cannot judge a
full run beside the runs it measures; Gemini Flash also works), so changing it is a config
change, not a code change. For each key point it returns yes/no and the sentence from the answer that covers it.
`results.md` names the judge's model id. `eval/human_grades.jsonl` holds the author's hand grades
for 10 random answers; the README reports judge–human agreement and says when the judge shares a
model family with a measured configuration.

**Acceptance:** results for at least `hosted` exist; a configuration that could not run is
listed as *not run* with the reason — never omitted, never estimated.

## 6. Explicitly out of scope

Voice, STT, TTS, avatar; a TypeScript or React front-end; integrating Vessel; a third-party agent
backend; running the loop on the client PC; memory consolidation; an MCP server of our own;
GitHub MCP; hosted tracing; multi-user auth beyond Cloudflare Access. Do not add interfaces
"ready for" these — the protocol (§5.3) and the seams in §5.1–5.2 are the preparation.

## 7. Definition of done

- [ ] `task verify` green locally (WSL2) and in CI
- [ ] Module boundaries enforced (a forbidden import fails a test)
- [ ] The adapter passes fixture tests, including parallel tool calls and cancellation
- [ ] A write tool cannot execute without an approval (test)
- [ ] Unlabelled MCP tools are not registered (test)
- [ ] `schema/events.schema.json` committed and drift-tested
- [ ] Retrieval ablation table committed (vector / keyword / hybrid / hybrid + rerank)
- [ ] `task eval` results committed for every configuration that could run
- [ ] `docker compose up` from a clean clone serves the console with hosted providers only
- [ ] A 2–3 minute demo video, on the public corpus and a demo Notion page only: a cited answer,
      a tool call, the route, and Approve → Notion write on localhost; linked from the README
- [ ] Public hostname works behind Access, in demo mode (a proposal is shown, its approval is
      refused), with llama-server not reachable from it — or listed as cut, with the reason
- [ ] README: architecture diagram, results table, protocol summary, public-corpus statement;
      Vessel named as the future client but **not linked** while it is private
- [ ] Whole history scanned for secrets before the repository is made public

## 8. Working conventions

- Branches: `feature/a0-bootstrap`, `feature/a1-chat-model`, `feature/a1-protocol-console`,
  `feature/a2-mcp`, `feature/a3-retrieval`, `feature/a4-eval`, `feature/deploy`.
- Commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`; present tense; no AI-attribution
  trailers.
- Report per PR: what was verified and how, what could not be (for example live
  `llama-server`), and where this spec was ambiguous.
