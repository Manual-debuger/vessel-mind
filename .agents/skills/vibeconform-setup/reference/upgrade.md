# Upgrading to a newer `vibe` or contract

## 1. Refresh the skill

The person installs the new `vibe` and runs `vibe init --skill`. It
rewrites `.claude/skills/vibeconform-setup/` and
`.agents/skills/vibeconform-setup/` and changes nothing else. Reload this
skill afterwards: the files you read before may have changed.

## 2. Compare the contract versions

- The repository's version is `contract:` in `vibe.yaml`.
- The new version is in the output of `vibe --version`:

  ```text
  vibe version <version> (contract <X.Y.Z>)
  ```

Compare the MAJOR parts (the X).

## 3a. Same MAJOR

1. Set `contract:` in `vibe.yaml` to the new version.
2. Update `vibe-guard` to the version of the new `vibe` (`SKILL.md`,
   step 5).
3. Update the setup action in the audit workflow: its `version` input,
   and its pinned SHA to the commit of the new release tag. Never use
   `latest`: the action refuses it. When the new `vibe` is not a release
   (`vibe --version` shows a pseudo-version or `dev`, and this skill
   names `latest`), there is no tag for it: keep the action on the
   newest release tag that
   `git ls-remote --tags https://github.com/Manual-debuger/VibeConform`
   lists, and install `vibe-guard` from the same commit as `vibe`.
4. Run the audit loop (`SKILL.md`, step 6). A new MINOR may add SHOULD
   rules; see `reference/rules.md`.

## 3b. New MAJOR

A new MAJOR may tighten a rule, add a MUST rule or add a guard corpus
case, and it may need new guardrail settings.

1. Read `reference/rules.md` for what changed, and tell the person.
2. Before you change `.claude/settings.json` or anything under
   `.claude/hooks/`, ask the person to run `vibe unlock` in their own
   terminal, and wait until they say it is done. Never run it yourself.
   The guard refuses those edits until then.
3. Update `vibe-guard`, then make the guardrail changes.
4. Set `contract:` in `vibe.yaml` to the new version, and update the
   setup action as in 3a.
5. Run the audit loop.

Your next Stop locks the guardrails again (`vibe-guard relock`).
