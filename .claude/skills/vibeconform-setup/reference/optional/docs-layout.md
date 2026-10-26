# Docs layout (optional)

A place for each kind of document, so that people and agents know where
to read and where to write. The contract does not read it. Create it
only when the person asks, and keep any layout the repository already
has: then name its directories in the `AGENTS.md` Knowledge list
(`reference/optional/workflow.md`) instead.

## Directories

- `docs/specs/`: what must be true. Problem, constraints, desired
  behavior and acceptance criteria, one file per feature.
- `docs/architecture/`: how the system works now.
- `docs/decisions/`: decision records (ADRs), why a significant choice
  was made.
- `docs/development/` (when wanted): build, test and contribute guides.
- `docs/operations/` (when wanted): deploy, run and incident docs.

## `docs/README.md`

```markdown
# Documentation

## Layout

- [`specs/`](specs/): what must be true. Problem, constraints, desired
  behavior and acceptance criteria, one file per feature.
- [`architecture/`](architecture/): how the system works now.
- [`decisions/`](decisions/): decision records (ADRs), why a significant
  choice was made.
```

## `docs/specs/README.md`

```markdown
# Specs

Write one file per feature, `docs/specs/<feature>.md` (a numeric prefix
such as `0001-` keeps them in order). Each spec says what must be true:
the problem, the constraints, the desired behavior and the acceptance
criteria. It does not say how to change the code; that is the plan.
```
