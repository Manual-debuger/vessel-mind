# Agent Instructions

## Verification

These are the repository's entrypoints. Run them; do not guess other
commands.

- `task verify`: the full gate (formatting, static analysis, tests). Run
  it before you say that work is done. CI runs it on every pull request.
- `task verify:fast`: the fast subset. Run it while you work. The Stop
  hook runs it at the end of every turn and refuses the stop once when it
  fails.
- `task fmt`: format the repository. `task fmt:check`: check formatting
  and write nothing.

Do not weaken a test, lint or type check to make a change pass.

Finish with a ledger, one line per check: PASS, FAIL or UNVERIFIED.

## Attribution

Do not cite or reference yourself, or any coding agent or AI model, in
anything you write to the repository or its tracker: no `Co-Authored-By`
trailers or "Generated with" lines in commit messages, pull requests or
issues, and no self-references in code, comments or docs.

One exception: a provenance note. Where a reader must know how content
was made in order to trust a measurement (eval data such as the golden
set, and the docs that describe it), say that a model drafted it and
who reviewed it. State the method; name no product.

## Guardrails

- `vibe-guard` checks every shell command and file edit before it runs
  (`.claude/settings.json`). It refuses destructive commands, edits of
  secret files and of the guard's policy. When it refuses a command you
  need, ask the person to run it; do not look for another spelling.
  `vibe-guard explain '<command>'` says which rule matched.
- Do not edit `.claude/settings.json` or `.claude/hooks/` unless the
  person has run `vibe unlock` in their own terminal. Never run
  `vibe unlock` yourself.
