# Developing vessel-mind

## Accepted advisories

### zod GHSA-mrhx-c7xc-m3w5 in `@notionhq/notion-mcp-server` 2.5.2

Accepted 2026-10-26.

- The pinned server bundles its own copy of zod, so an npm override
  cannot replace it.
- Its only input is the runtime's own tool calls, sent over stdio.
- The server is never on the network: it has no listening port and is
  not reachable through the tunnel.
- Revisit when a release of the server ships zod 4.
