# The rules of contract 1.1.0

One entry per rule: its level, what satisfies it, and the reference file
to start from. Keep anything that already satisfies a rule. The audit is
the judge: `vibe audit --explain <ID>` prints a rule's exact check.

"Called" means a hook command, CI `run:` step, GitLab `script` or
`before_script` line, or entrypoint that, parsed as shell, contains the
declared entrypoint command as a whole simple command. For a list
entrypoint, every command in the list must be called.

`vibe.yaml` declares the entrypoints `verify`, `verify-fast`, `fmt` and
`fmt-check`, and optionally `setup`. Start from
`reference/vibe-yaml/<stack>.yaml`, where `<stack>` is `go`, `python`,
`typescript` or `monorepo`.

## V: verification entrypoints

### V-01 (MUST): `verify` covers formatting, static analysis and tests

`verify` exits 0 on the clean tree, and non-zero with each planted defect
in each component: an unformatted file, a static-analysis defect (Go:
a `fmt.Printf` argument mismatch that `go vet` catches; Python and
TypeScript: a reference to an undefined name) and a failing test. Every
component needs at least one test file: Go `_test.go`, Python `test_*.py`
or `*_test.py`, TypeScript `*.test.ts`, `*.spec.ts`, `*.test.tsx` or
`*.spec.tsx`. Start from `reference/taskrunner/<stack>/` (`verify`).
Python needs a type checker or linter that reports undefined names
(ruff's `F821`, pyright); TypeScript needs `tsc --noEmit` or ESLint's
`no-undef`.

A component with no test runner (a `test` script that only echoes, say)
gets one, and one real test of its code. V-01 plants its failing test
beside the component's first test file, so the runner must pick up
every test file there:

- Go: `go test ./...`, built in.
- TypeScript: Node's built-in runner, `node --test "src/**/*.test.ts"`
  (Node 22.18 or newer runs `.ts` files). A test that imports `./x.ts`
  needs `rewriteRelativeImportExtensions` (or
  `allowImportingTsExtensions`) in `tsconfig.json`. The planted test
  throws when loaded, which fails the run.
- Python: pytest (`uv add --dev pytest`). Not `python -m unittest`: it
  finds only `test*.py` files and `TestCase` classes, so it misses the
  planted `vibe_probe_test.py` and its plain `test_` function, and V-01
  fails.

### V-02 (MUST): `verify-fast` catches the same defects

Each planted defect makes `verify-fast` exit non-zero. Keep the format
check, the static analysis and the tests in it; leave out the slow,
network or whole-repository checks. Start from
`reference/taskrunner/<stack>/` (`verify:fast`).

### V-03 (MUST): `fmt-check` writes no file

`fmt-check` checks formatting and changes nothing: `gofmt -l`,
`ruff format --check`, `prettier --check`. Start from
`reference/taskrunner/<stack>/` (`fmt:check`).

### V-04 (MUST): the entrypoints are declared and used

`vibe.yaml` declares `verify`, `verify-fast`, `fmt` and `fmt-check`, each
non-empty. Use the team's runner: `make verify`, `just verify` or
`npm run verify` are as good as `task verify`.

## H: local Git hooks

Recognized hook managers: lefthook (`lefthook.yml`, `.lefthook.yml`),
pre-commit (`.pre-commit-config.yaml`) and husky (`.husky/`). Keep the
one the repository has.

### H-01 (SHOULD): hook configuration is committed

Commit the hook manager's configuration. Start from
`reference/hooks/<stack>/lefthook.yml` when there is none.

### H-02 (SHOULD): pre-commit checks formatting, pre-push runs verification

The pre-commit hook calls `fmt-check` or `fmt`; the pre-push hook calls
`verify-fast` or `verify`. Start from `reference/hooks/<stack>/lefthook.yml`.

## C: CI

The C rules read GitHub Actions (`.github/workflows/`) or GitLab CI
(`.gitlab-ci.yml`). GitHub wins: with tracked workflows, `.gitlab-ci.yml`
is not read. The report says which provider it read. A repository with
another provider's CI gets "cannot decide": keep its CI, make it call
the entrypoints, and report it. Keep the provider the team has.

On GitLab CI, the audit reads `.gitlab-ci.yml` as GitLab resolves it:
anchors, `include: local` files, `default:`, `extends:` and `!reference`.
Keys starting with `.` are templates, not jobs. Includes of other kinds
are not followed. A child pipeline (`trigger: include:` of local files)
is read only with `strategy: depend`, under the trigger job's rules. A
job's commands are its `before_script` and `script` lines; `after_script`
does not count as calling `verify`. Start from
`reference/ci/<stack>/.gitlab-ci.yml`.

The audit asks of each GitLab job: does it run in merge request
pipelines, and on pushes to the default branch? It evaluates
`workflow:rules`, then the job's `rules:` or `only`/`except`. It reads
these conditions: `$CI_PIPELINE_SOURCE ==` or `!=` a quoted value,
`$CI_MERGE_REQUEST_IID`, `$CI_COMMIT_BRANCH` and `$CI_COMMIT_TAG` as
presence tests, `$CI_COMMIT_BRANCH` or `$CI_COMMIT_REF_NAME` compared with
`$CI_DEFAULT_BRANCH` or a branch name, and `&&`, `||` and parentheses
over these. A rule with another condition, or with `changes:` or
`exists:`, does not admit the job. With `when: never` it leaves the job
out; without, a later rule may still admit it. When none does, the
finding quotes it and its line. `workflow:rules` and include rules read
the same way.
A job with no rules counts for both, but GitLab does not run it in a
merge request pipeline: with `workflow:rules` that admit merge request
pipelines, give every job the same rules, as the reference does with
`extends: .pipelines`.

### C-01 (MUST): CI runs `verify` on pull requests and pushes

A workflow triggered by `pull_request` and by `push` to the default
branch calls `verify`. Start from `reference/ci/<stack>/ci.yml`.

GitLab: jobs that run in merge request pipelines call every `verify`
command, and so do jobs that run on pushes to the default branch. A job
gated by `changes:` does not count, as a GitHub `paths` filter does not.

### C-02 (MUST): toolchains and actions are pinned

In every workflow, every `uses:` other than a local path is pinned to a
full 40-character commit SHA (keep the tag in a comment), and no `run:`
step has a command word ending in `@latest`. Find a tag's SHA with
`git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>`. The
reference workflows are pinned.

GitLab: every `image:` and `services:` entry, in jobs and `default:`,
ends in `@sha256:` and the 64-character digest (keep the tag before it:
`golang:1.27.0-bookworm@sha256:<digest>`). Find the digest with
`docker buildx imagetools inspect <image>:<tag>` (the `Digest:` line).
Every include the audit does not follow is pinned: `project:` with a
`ref:` of a full commit SHA, `component:` with `@<full commit SHA>`,
`remote:` with `integrity:`. A `template:` include cannot be pinned and
fails: copy the template into the repository and include it with
`local:`, or include the matching component pinned to a SHA. No word of
a `script`, `before_script` or `after_script` line ends in `@latest`.

### C-03 (MUST): workflow permissions default to read

Every workflow has top-level `permissions` that grant no write scope:
`contents: read`, `read-all` or `{}`. A job may widen them.

GitLab: not applicable. A GitLab job token has no per-file permissions,
and its push access is a project setting (below).

### C-04 (MUST): the audit runs in its own job

A job triggered on pull requests runs `vibe audit` (after the setup
action) and does not call `verify`. Start from
`reference/ci/<stack>/audit.yml`. Replace the zero SHA of
`Manual-debuger/VibeConform` with the commit of the release tag.

GitLab: a job that runs in merge request pipelines has a `script` line
starting with `vibe audit` and calls no `verify` command. Start from the
`conformance:audit` job of `reference/ci/<stack>/.gitlab-ci.yml`, which
installs `vibe` and `vibe-guard` from the release and checks them against
its `checksums.txt`. Set `VIBE_VERSION` to an exact release tag.

### C-05 (SHOULD): tests run on Linux and Windows

The steps that call `verify` run on a Linux and a Windows runner after
matrix expansion. Not applicable when `platforms` has no `windows`. The
reference `ci.yml` has an `os` matrix.

GitLab: a job's platform comes from its `tags:`, after `parallel: matrix`
expansion. A tag containing `windows` is Windows, `linux` Linux, `macos`
macOS (case-insensitive). A job with no tags and an `image:` is Linux.
On GitLab.com the Windows runners take `tags: [saas-windows-medium-amd64]`;
the reference has a `verify:windows` job. A self-managed instance names
its own runner tags.

### GitLab project settings the audit cannot read

The audit reads files, not GitLab's project settings. Tell the person to
set these in the project's settings, as GitHub branch protection is set:

- **Pipelines must succeed** (Settings, Merge requests, Merge checks), so
  that a red `verify` or `conformance:audit` job blocks the merge.
- **Protected branches** (Settings, Repository): protect the default
  branch, with "Allowed to push and merge" set to no one, or to
  Maintainers, and force push off, so that changes land through merge
  requests.
- **Job token push access** (Settings, CI/CD, Job token permissions):
  keep "Allow Git push requests to the repository" off, so that a job
  cannot push to the repository. This is what C-03 checks on GitHub.

## A: agent guardrails (Claude Code)

Not applicable when `agents` does not include `claude`. Start from
`reference/agents/settings.json` and `reference/agents/policy.json`:
`vibe-guard` satisfies A-01 to A-04 and A-08. Install it first (`SKILL.md`,
step 5).

The hooks run from the project root. Claude Code runs a hook command in
the agent's current directory, and Task uses the nearest `Taskfile.yml`
above that directory. Inside a component, or under any directory with
its own Taskfile, that is not the root Taskfile: the `hook:*` tasks are
missing there, and `verify:fast` checks only that part of the
repository. So every reference hook command that calls Task starts with
`cd "${CLAUDE_PROJECT_DIR:-.}" &&`. Keep this prefix, and give it to
each hook of your own that calls the task runner. Do not use
`task -d <dir>` instead: A-05 finds the Stop hook's `task verify:fast`
only as written. `vibe-guard hook` finds the root itself and needs no
prefix.

### A-01 (MUST): shell commands and file edits pass through the guard

`.claude/settings.json` has PreToolUse hooks matching `Bash`,
`PowerShell`, `Write`, `Edit` and `MultiEdit`.

### A-02 (MUST): the guard refuses the baseline

Every deny case of the guard corpus is refused (exit 2) or let through by
a listed exception, and every allow case is allowed. `vibe-guard hook`
does this.

### A-03 (MUST): the guard fails closed

Deny cases are still refused with `PATH` reduced to the system's
directories and with `.claude/hooks/policy.json` invalid. The reference
PreToolUse command exits 2 when `vibe-guard` is missing.

### A-04 (MUST): the guard is loosened only through listed exceptions

`.claude/hooks/policy.json` is valid version 2. Each entry of
`exceptions` names a guard rule ID (`rm-rf`, `remove-item-recurse-force`,
`git-reset-hard`, `git-clean-fd`, `git-push-force`,
`git-branch-force-delete`, `git-discard-worktree`, `secret-file`,
`guard-policy-file`, `guardrail-settings`), a `match` and a non-empty
`reason`. Add no exception the person has not asked for.

### A-05 (MUST): the agent's turn ends with `verify-fast`

A Stop hook calls `verify-fast`, exits 2 when it fails for a payload with
`stop_hook_active: false`, and exits 0 for `stop_hook_active: true`. The
reference Stop command and the `hook:done` task in
`reference/taskrunner/<stack>/Taskfile.yml` do this.

### A-06 (SHOULD): edited files are formatted

A PostToolUse hook matching `Write` and `Edit` calls the `fmt`
entrypoint as written in `vibe.yaml`. The reference
`.claude/settings.json` runs `task fmt 1>&2 || exit 2` from the project
root.

### A-07 (SHOULD): session start injects facts and changes nothing

A SessionStart hook exists, exits 0 and leaves `git status --porcelain`
empty (`hook:context` in the reference Taskfiles).

### A-08 (MUST): guardrail settings change only after a person unlocks them

The guard refuses edits of `.claude/settings.json` and `.claude/hooks/`
without a valid unlock marker, refuses `vibe unlock` and the marker's
creation, and the Stop hook removes the marker (`vibe-guard relock`).
`vibe-guard` and the reference Stop command do this.

## D: dependencies and supply chain

### D-01 (MUST): lockfiles are committed and match

Per component: `go.sum` (tidy), `uv.lock`, `poetry.lock`,
`pnpm-lock.yaml` or `package-lock.json`, committed and current. A
component with dependencies and no recognized lockfile fails. Run the
package manager's lock command and commit the result.

### D-02 (SHOULD): an update bot covers dependencies and actions

`.github/dependabot.yml` or a Renovate configuration covers each
component's ecosystem and `github-actions`, with no automerge. Start from
`reference/ci/<stack>/dependabot.yml`.

GitLab: Dependabot does not run on GitLab, so it cannot satisfy D-02
there. A Renovate configuration (`renovate.json`, `renovate.json5`,
`.gitlab/renovate.json` and the other Renovate locations) covers each
component's manager and `gitlabci`, which updates the image digests, with
no automerge. When it lists `enabledManagers`, add `gitlabci`, and
`gitlabci-include` when `.gitlab-ci.yml` has `project:` includes (the
ones that manager updates). Start from `reference/ci/renovate.json`; Renovate's
`config:recommended` enables every manager. The person runs Renovate on
the project (the Mend app, or a scheduled Renovate pipeline).

### D-03 (SHOULD): CI scans for known vulnerabilities

A workflow runs `govulncheck`, `pip-audit`, `pnpm audit`, `npm audit`,
`osv-scanner` or `trivy`. The reference `ci.yml` does.

GitLab: a job's script runs one of those scanners, as the reference
`security` job does, or the pipeline includes GitLab's dependency
scanning: the `dependency-scanning` CI/CD component, pinned to a commit
SHA, or the Dependency-Scanning template. The template fails C-02 (it
cannot be pinned), so prefer a scanner command or the pinned component.

## R: removability and repository hygiene

### R-01 (MUST): everything but the audit works without vibe

No entrypoint, hook command or CI step calls `vibe`, except the audit job
of C-04. `vibe-guard` is not `vibe`, so the guard hooks are fine. A CI
job that calls no entrypoint may run `vibe --version` to check an
installed binary. On GitLab, every job's `before_script`, `script` and
`after_script` is read, except the `conformance:audit` job's.

### R-02 (SHOULD): agents are told the verification entrypoints

`AGENTS.md` contains every `verify` command as written, and `CLAUDE.md`
references `AGENTS.md`. Start from `reference/agents/AGENTS.md` and
`reference/agents/CLAUDE.md`.

### R-03 (SHOULD): pull requests report verification

A pull request template has a heading containing "verif". Start from
`reference/ci/pull_request_template.md`.

GitLab: every `.md` file directly under `.gitlab/merge_request_templates/`
is a template too. Put the reference there as
`.gitlab/merge_request_templates/Default.md`, which GitLab uses for new
merge requests.
