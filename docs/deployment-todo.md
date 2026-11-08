# Deployment TODO

Goal: run the shopping assistant on the home Plex server, add a page for issuing agent prompts,
protect it with TLS and authentication, and leave room for external access later.

Target shape:

```
 Home LAN / Tailscale / (later) Cloudflare Tunnel
                 │  HTTPS
          ┌──────▼──────┐
          │   Caddy     │  TLS + optional edge auth
          └──────┬──────┘
     ┌───────────┴────────────┐
┌────▼─────────┐        ┌─────▼─────────┐
│ shopping app │◄─MCP───│ agent runner  │──► Anthropic API
│ UI + MCP +   │        │ (Agent SDK)   │──► Notion MCP (integration token)
│ SQLite       │        └───────────────┘
└──────────────┘
```

## Decisions needed first

- [ ] **Server platform.** What OS does the Plex server run (Linux / Unraid / Synology / Windows), and is Docker available?
- [ ] **Domain.** Is there a domain to use, and is its DNS on Cloudflare? (Needed for browser-trusted TLS via DNS-01 and later for Cloudflare Tunnel.)
- [ ] **Agent billing.** Use an Anthropic API key (pay per use) for the prompt page?
- [ ] **Users.** Just Ryan, or other household members? (Single-user login vs. accounts.)
- [ ] **Remote access.** Tailscale only, or a public URL via Cloudflare Tunnel? (Can wait until phase 3.)

## Phase 1 — LAN deployment with TLS and login

- [ ] Add a Caddy service to `docker-compose.yml` (reverse proxy for the app; the app stops publishing its own port).
- [ ] TLS: Let's Encrypt certificate via DNS-01 (Cloudflare API token scoped to the one zone), so no inbound ports are needed.
      Fallbacks: Tailscale `serve` certificates, or Caddy `tls internal` (requires trusting the CA on every device).
- [ ] Local DNS: point `shop.<domain>` at the server's LAN IP (router override or Pi-hole/AdGuard record).
- [ ] App login: single-user password (argon2 hash in `.env`), secure / HttpOnly / SameSite session cookie, optional TOTP. Keep the existing CSRF origin check.
- [ ] Set `PUBLIC_URL`, `ALLOWED_HOSTS` and `MCP_TOKEN` for the new hostname; require the MCP bearer token.
- [ ] Trust proxy headers only from Caddy (`--forwarded-allow-ips`).
- [ ] Backups: nightly `sqlite3 data/app.db ".backup ..."` (the DB is in WAL mode, so don't copy the file directly) to the server's backup location.
- [ ] Health check and restart policy confirmed; logs rotated.
- [ ] Verify the stack doesn't collide with Plex's ports (32400) or anything else already on 80/443.
- [ ] Migrate the current `data/app.db` from the dev VM, or start fresh (drop the test runs and test feedback).

## Phase 2 — Agent runner and Prompts page

- [ ] Notion internal integration: create it, share **only** the Groceries and Kitchen Pantry and Storage databases with it.
      Read-only unless the Skipped/Purchased writes are wanted.
- [ ] `agent-runner` container: Claude Agent SDK with exactly two tool sources — the shopping MCP (bearer token) and Notion's MCP server (integration token).
      No shell, filesystem, web fetch or other built-in tools.
- [ ] Load the `grocery-shopping` skill as the runner's instructions.
- [ ] Prompts page in the app: free-text prompt plus saved prompts ("Weekly shop", "What regular items are due?").
- [ ] Stream the agent's output to the page; store each run's transcript, tool calls, token usage and cost, linked to the shopping run it created.
- [ ] Guardrails: max turns, max tokens/cost per run, one run at a time, cancel button.
- [ ] Optional schedule (e.g. Thursday 7 pm: build the weekly run, then notify).
- [ ] Secrets (Anthropic key, Notion token, MCP token) only in the runner's env / Docker secrets; never shown in the UI or logs.
- [ ] Note: the retailer token's 1-hour life is fine for unattended runs — staging needs no login; the token is pasted when approving.

## Phase 3 — Remote access (decide later)

- [ ] **Tailscale** (just Ryan's devices): run Tailscale on the server, use `tailscale serve` for HTTPS on the tailnet. No open ports.
- [ ] **Cloudflare Tunnel** (normal public URL): `cloudflared` container; Cloudflare Access (email OTP or SSO) in front of the UI; keep app login on as well.
- [ ] If using the claude.ai custom connector instead of / alongside the runner: expose `/mcp` publicly and add OAuth to the MCP endpoint (claude.ai can't pass Cloudflare Access or a static bearer token). Exempt only `/mcp` from Access.
- [ ] Never port-forward from the router.

## Open questions / follow-ups

- [ ] Browser-assisted token refresh (extension or bookmarklet) so the retailer session lasts longer than an hour.
- [ ] Notifications when a run is ready for review (email, ntfy, Pushover, …).
- [ ] Mobile layout pass for the review/triage pages once it's reachable from a phone.
