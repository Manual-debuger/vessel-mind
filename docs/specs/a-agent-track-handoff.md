# Agent track: hand-off

The state of [the agent track spec](a-agent-track.md) (#3) when its integration branch
`spec/3-agent-track` was marked ready in PR #21 (2026-10-27). Every ticket (#4–#17) is
merged, a code review of the whole branch is fixed, and `task verify` and CI (ubuntu and
windows) are green. What remains needs the author's accounts, hardware or a browser:
the [manual test plan](#manual-test-plan) and the
[checklist before the repository is made public](#before-the-repository-is-made-public).

## Decisions

These are settled; the PR body and the README record them too.

- **Hosted models:** `gemini-3.5-flash-lite` (fast) and `gemini-3.6-flash` (slow; Google
  routes `gemini-3.5-flash` to it). The slow path falls back to `gemini-3.5-flash-lite`.
  The free tier allows 20 `gemini-3.6-flash` requests a day.
- **Local models (Ollama on the GPU host, port 11434):** `qwen3:8b` (fast, with
  `reasoning_effort: none`: with thinking it takes about 10 s to choose a route, the
  router's timeout) and `qwen3.6:35b-a3b` (slow). Both stay loaded on a 32 GB GPU only
  with `OLLAMA_CONTEXT_LENGTH=16384` and no other model resident; otherwise Ollama
  swaps them every turn and the turn latency measures the swap.
- **Eval:** free models only. Table 2 has two measured rows, `local` and
  `local-slow-only`, judged by `qwen3.6:35b-a3b`; `hosted`, `slow-only` and `routed`
  are *not run* (the 20-requests-a-day limit; a row needs about 100). The author graded
  10 sampled answers: judge and author agree on 30 of 32 key points.
- **Reranker:** the `llama-server` provider is cut; `cpu` and `none` ship.
- **Layers:** `eval > server > agent|rag > core`; `eval` is a second composition root.
- **Golden set:** `eval/golden.yaml`, frozen; change it only under
  `eval/golden-changes.md`.
- **Accepted advisory:** GHSA-mrhx-c7xc-m3w5 in `@notionhq/notion-mcp-server` 2.5.2,
  recorded in `DEVELOPING.md`.
- **Branch protection:** not applied: the repository is private on GitHub Free, where
  protection returns 403.

## Re-running the eval

Results in `eval/results/` are generated; never edit them by hand. From a checkout
with the index built (`task ingest`) and Ollama serving both local models:

```sh
task eval -- --retrieval-only                 # table 1, CPU only
task eval -- --config local --fresh           # GPU; about 25 minutes
task eval -- --config local --no-router --fresh
task eval -- --grade                          # the author grades 10 answers
task eval -- --render                         # regenerate results.md
```

A row resumes when its command is run again; `--fresh` discards it. A golden-set or
model change needs a full re-run (see `eval/golden-changes.md`). Commit
`eval/results/` and `eval/human_grades.jsonl` together.

## Manual test plan

None of these has run. Each needs a person, an account or a browser. Screenshots must
show only the public corpus and demo pages, never personal data.

1. **Console in a browser (`task dev`, `hosted`).** Ask an English question: the text
   streams, the route badge names the route and both models, and each `[n]` opens its
   source. Cancel a turn mid-answer: it stops at once. Reload the page: the session is
   kept. Turn the network off in the browser's developer tools mid-turn, then on again:
   the status shows the disconnect, and the answer is not repeated after the reconnect.
2. **Citations (#11).** One English and one Traditional Chinese question, each with
   `[n]` links to the right documents. Take screenshots.
3. **Fast path, then slow path, on Gemini.** In one session, ask a fast question, then a
   follow-up that takes the slow path. The slow turn succeeds (no HTTP 400): the stored
   fast-path search has no Gemini thought signature, which Gemini should accept for an
   earlier turn.
4. **Fallback.** Set the slow model to an id that fails (or wait until its daily quota is
   spent): the `route` event names `gemini-3.5-flash-lite`, the model that answered.
5. **Daily turn cap.** Set `sessions.max_turns_per_day: 2`: the third turn is refused
   with 429 `daily_turn_limit`, also from a new session.
6. **Telemetry.** `data/state/telemetry.jsonl` has a route line and a turn line for each
   turn, and no message text.
7. **Proposal and approval (#9).** With a Notion token and a demo page, ask for a write:
   a proposal card appears; Approve runs it once; the next turn knows it was approved.
   Reject one: the next turn knows it was rejected. Watch for Gemini refusing the Notion
   tool schemas (`$ref`/`oneOf`) with a 400.
8. **Local configuration in the app.** `task dev` with `configuration: local`: a turn
   answers through Ollama.
9. **Containers.** From a clean clone, `docker compose up`, then one live `hosted` turn
   inside the container.
10. **Public host (#16).** The Cloudflare tunnel (`--profile tunnel`, `TUNNEL_TOKEN`), the
    Access email PIN, the public-host notice in the console, demo mode (every approval
    is refused with 403), the read-only Notion integration, and the daily cap of 100.
11. **Demo video,** linked from the README, after 2 and 7.

## Before the repository is made public

- [x] Scan the whole git history for secrets, for example
      `gitleaks git --log-opts="--all"`. A finding means: rotate that key first, then
      rewrite the history. Done 2026-10-29 with gitleaks v8.24.3: no leaks in 48 commits.
- [ ] No personal data in the repository: README, screenshots, `eval/results/`,
      `eval/human_grades.jsonl`, test fixtures. `config/agent.yaml` and env files never
      committed (`git log --all -- config/agent.yaml` prints nothing).
- [ ] Vessel is named but not linked while it is private (`tests/test_readme.py`
      checks it).
- [ ] The public-host config is `config/agent.public.example.yaml`: `demo_mode: true`,
      `public_host: true`, `sessions.max_turns_per_day` set, Notion read-only.
- [ ] Manual tests 1–11 above done, or listed as open in the README's definition of
      done.
- [x] PR #21 merged into `main`; the tickets close with it.
- [ ] After the repository is public, protect `main` (GitHub flow): require a pull
      request and the checks `verify (ubuntu-latest)`, `verify (windows-latest)`,
      `security` and `audit` (strict); no force push or deletion; include admins; no
      required approvals for a single owner.
