# Language servers (optional)

Language servers help a person or an agent navigate and check code while
it is being written. They never replace the `verify` entrypoint, and no
entrypoint, hook or CI step needs them. Suggest them only when the person
asks.

Use servers that read the same configuration as the repository's checks,
so that the editor and `verify` agree:

| Language | Server | Reads |
|---|---|---|
| Go | `gopls` | `go.mod`; `golangci-lint` stays in `verify` |
| Python | `pyright` (or `basedpyright`), `ruff server` | `pyrightconfig.json`, `ruff.toml` |
| TypeScript | `typescript-language-server`, the ESLint server | `tsconfig.json`, `eslint.config.js` |

- Install them on the developer's machine, never as a repository
  dependency that `verify` needs.
- Claude Code can use language servers through its LSP plugins. That is
  a personal setting, not part of the committed configuration.
