# Zed (optional)

The contract does not read editor configuration. Write this only when
the person asks, and merge it into an existing `.zed/tasks.json`.

## `.zed/tasks.json`

One task per entrypoint. Use the repository's real entrypoint commands.

```json
[
  {"label": "task fmt", "command": "task", "args": ["fmt"]},
  {"label": "task lint", "command": "task", "args": ["lint"]},
  {"label": "task test", "command": "task", "args": ["test"]},
  {"label": "task verify", "command": "task", "args": ["verify"]}
]
```

Zed starts the language servers it knows for Go, Python and TypeScript
by itself; see `reference/optional/lsp.md`.
