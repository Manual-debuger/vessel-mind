# VS Code (optional)

The contract does not read editor configuration. Write these only when
the person asks. Merge them into existing files: keep the team's own
tasks and recommendations.

## `.vscode/tasks.json`

One task per entrypoint, so that the editor runs what CI runs. Use the
repository's real entrypoint commands.

```json
{
  "version": "2.0.0",
  "tasks": [
    {"label": "task fmt", "type": "shell", "command": "task", "args": ["fmt"], "problemMatcher": []},
    {"label": "task lint", "type": "shell", "command": "task", "args": ["lint"], "problemMatcher": []},
    {"label": "task test", "type": "shell", "command": "task", "args": ["test"], "problemMatcher": []},
    {"label": "task verify", "type": "shell", "command": "task", "args": ["verify"], "problemMatcher": []}
  ]
}
```

## `.vscode/extensions.json`

The extensions each language's tooling needs. Keep only the languages
the repository has.

```json
{
  "recommendations": [
    "golang.go",
    "dbaeumer.vscode-eslint",
    "esbenp.prettier-vscode",
    "charliermarsh.ruff",
    "ms-python.python"
  ]
}
```

- Go: `golang.go`.
- TypeScript: `dbaeumer.vscode-eslint`, `esbenp.prettier-vscode`.
- Python: `charliermarsh.ruff`, `ms-python.python`.
