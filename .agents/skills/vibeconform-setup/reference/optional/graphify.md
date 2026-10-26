# Graphify (optional)

Graphify keeps a knowledge graph of the repository in `graphify-out/`.
It is derived and machine-local output. Set it up only when the person
asks. It never replaces verification, and nothing in `verify`, the hooks
the contract reads or CI may depend on it.

## Files

- `.gitignore`: add `graphify-out/`.
- A `graph:update` task that rebuilds the graph, and skips when graphify
  is not installed:

  ```yaml
  graph:update:
    desc: Rebuild the Graphify graph in graphify-out/ (run by the post-commit, post-checkout and post-merge hooks)
    silent: true
    cmds:
      - |
        if ! command -v graphify >/dev/null 2>&1; then
          echo "graphify: not on PATH; graph not updated, graphify-out/ may be stale (install: uv tool install graphifyy)" >&2
          exit 0
        fi
        graphify update . || echo "graphify: update failed; graphify-out/ may be stale" >&2
  ```

  Never call it from pre-commit or pre-push: a missing or failing
  graphify must not hold up a commit or a push.

- Git hooks that run it after the tree changes, for example in
  `lefthook.yml`:

  ```yaml
  post-commit:
    jobs:
      - name: graph-update
        run: task graph:update
  post-checkout:
    jobs:
      - name: graph-update
        run: task graph:update
  post-merge:
    jobs:
      - name: graph-update
        run: task graph:update
  ```

- `AGENTS.md`, a short paragraph:

  ```markdown
  Repository intelligence:

  - Graphify keeps a knowledge graph in `graphify-out/`. Use it only when
    `built_at_commit` in `graph.json` matches HEAD (`task graph:update`
    rebuilds it); otherwise, or when it is absent, use search, the
    compiler and tests. It never replaces verification.
  ```

Install graphify with `uv tool install graphifyy` or
`pip install graphifyy`.
