# ADR 0002 — Expose the demo through Cloudflare Tunnel and Access

- **Status:** Accepted · amended 2026-10-26 (see [Amendments](#amendments))
- **Date:** 2026-10-25
- **Related:** [0001 agent runtime](0001-an-agent-runtime-separate-from-embodiment.md)

## Context

The demo must be reachable from a phone and from a reviewer's browser, over HTTPS (browsers
require a secure context for the microphone, which Vessel will need later). It runs on a home
machine with a GPU and a domain whose DNS is on Cloudflare. It drives a paid model API and can
reach the author's tools, so an open URL is a spending and data risk.

## Decision

**Publish only the app server through a Cloudflare Tunnel, behind Cloudflare Access.**

| Part | Decision |
|---|---|
| Ingress | `cloudflared` runs as a compose service and opens an outbound tunnel. No inbound port is opened on the home network. |
| What is exposed | The app server only (HTTP, including its Server-Sent Events stream). `llama-server`, the database and MCP servers stay on the internal network and are never routed through the tunnel. |
| Authentication | Cloudflare Access in front of the hostname; one-time PIN sent to any email address (no allow-list). The app does not implement its own login in this track. |
| TLS | Terminated by Cloudflare with a certificate for the real hostname. |
| Demo mode | On the public hostname, only read tools execute. The loop still makes proposals for write tools, and the console shows them. The server refuses every **approval** with a "demo mode" error, so no write tool executes there. |
| Spend | The hosted model key belongs to a project with **no billing account**, so it cannot spend money. A per-session turn limit protects the free quota. |
| Secrets | The tunnel token is an environment variable, never in a committed file. |

## Consequences

- A reviewer gets a link that works, after an email PIN.
- Every request passes through Cloudflare; for one user and a demo this is acceptable.
- The app must not trust a client-supplied identity; if it needs the user's identity later, it
  reads and verifies the Access JWT header.
- Local development needs no tunnel: `localhost` works without it.

## Alternatives considered

**Port-forward on the home router.** Exposes the home IP, needs its own TLS and auth, and makes
the router part of the threat model. Rejected.

**A private mesh VPN (for example Tailscale).** Strong for the author's own devices, but a
reviewer would have to join the network to see the demo. Rejected for the demo; nothing stops the
author from also using one privately.

**Tunnel without Access.** Simplest link to share, but anyone with the URL spends the model
budget and reaches the tools. Rejected.

## Amendments

**2026-10-26: demo mode refuses approvals, not proposals.** The first version disabled write
proposals on the public hostname. A reviewer then never saw the approval flow, which is the
track's main safety claim. Now the public hostname shows proposals and refuses only the approval.
The recorded demo on `localhost` shows the full flow: Approve, then the Notion write.
Rejected options: no proposals on the public hostname (the reviewer cannot see the flow), and
real approvals to a sandbox Notion page (any allow-listed reviewer could write to the author's
workspace).

**2026-10-26: no billing account instead of a spend cap.** ADR 0001 (amended the same day) moves
the hosted models to a free tier with no paid key. The Spend row now relies on a key in a project
with no billing account, so the risk of an open URL is an exhausted free quota, not a bill.

**2026-10-26: the public host's Notion token is read-only.** Refusing approvals is one check in
the app; a bug there would open a write path to the author's Notion. The public host therefore
uses a Notion integration with only the "Read content" capability, so Notion itself refuses a
write. The localhost recording uses a second integration with write rights. Each integration is
shared with one demo page only, never a personal page. Write tools stay registered on the public
host, so proposals are still shown. Rejected options: one token guarded only by the app's check,
and no Notion on the public host (no proposal to show).

**2026-10-26: any email may sign in; the public URL is optional.** An allow-list locks out the
reviewer who clicks the link from an application, because their address is not known in advance.
Access now sends a one-time PIN to any email address. Cost is still bounded by the free-quota key
with no billing account and by the per-session turn limit, and writes are still impossible on the
public host. The track now treats a demo video as required and the public URL as the last item in
its cut order; this ADR decides how the URL is exposed if it ships. Rejected options: keep the
allow-list (reviewers cannot get in), and drop the public URL entirely (loses a live link for
interviews when time allows).
