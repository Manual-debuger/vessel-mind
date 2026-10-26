# `AGENTS.md` workflow sections (optional)

The contract leaves the development workflow to the team. When the
person asks for one, add one of these sections to `AGENTS.md`, below the
Verification section of `reference/agents/AGENTS.md`. Each is short on
purpose: it routes to the documents and commands that hold the detail.
Replace the directories with the repository's own; with no docs layout,
use the "no layout" knowledge text.

## Knowledge

With a docs layout (`reference/optional/docs-layout.md`):

```markdown
Knowledge:

- Specs (what must be true) live in `docs/specs/`, architecture (how it
  works now) in `docs/architecture/`, decisions (ADRs) in
  `docs/decisions/`. Read the relevant ones before a non-trivial change.
- If an approved spec, an ADR and the code disagree, say so. Do not
  pick one silently.
```

No layout:

```markdown
Knowledge:

- Read the specs, architecture docs and ADRs that apply before a
  non-trivial change.
- If an approved spec, an ADR and the code disagree, say so. Do not
  pick one silently.
```

## `direct`

```markdown
## Repository workflow

Workflow: direct.

- Implement, then verify. Respect any spec that applies.
- A planning context (the harness's plan mode, or a request for a spec)
  writes a lightweight spec with acceptance criteria when asked. The
  spec says WHAT must be true; the plan says HOW to change the
  repository.
```

## `plan-triggered-sdd`

```markdown
## Repository workflow

Workflow: plan-triggered lightweight SDD.

- Normal mode: implement, then verify. Respect any spec that applies.
- Planning context (the harness's plan mode, or a request for a spec):
  list the constraints that apply and the assumptions you have not
  verified, then write a lightweight spec with acceptance criteria. Plan
  only after that.
- The spec says WHAT must be true; the plan says HOW to change the
  repository. Keep them apart. Reuse an approved spec when one exists.
- In a read-only plan mode, put the spec in the plan. Once it is
  approved, write it to `docs/specs/` first.
- Do not implement until the user approves.
```

## `always-sdd`

```markdown
## Repository workflow

Workflow: spec-driven.

- A non-trivial behavioural change needs an approved spec with
  acceptance criteria before it is planned, in any mode. A small fix
  may go straight to implement and verify.
- Planning context (the harness's plan mode, or a request for a spec):
  list the constraints that apply and the assumptions you have not
  verified, then write a lightweight spec with acceptance criteria. Plan
  only after that.
- The spec says WHAT must be true; the plan says HOW to change the
  repository. Keep them apart. Reuse an approved spec when one exists.
- In a read-only plan mode, put the spec in the plan. Once it is
  approved, write it to `docs/specs/` first.
- Do not implement until the user approves.
```

Put the Knowledge list after the section heading, before "Workflow:".
