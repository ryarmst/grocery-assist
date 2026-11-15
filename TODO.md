# TODO

Suggested next steps, roughly in priority order.

## 1. Deployment: built-in TLS and login

- [ ] **Built-in TLS, zero config.** Add an optional Caddy service to `docker-compose.yml` (a Compose
      profile) so `docker compose --profile tls up -d` serves HTTPS:
      - with a domain: automatic Let's Encrypt via DNS-01 (e.g. Cloudflare API token) — no open ports;
      - without one: `tls internal` (self-signed CA; trust it once per device).
      Alternative with no proxy: let the app load `TLS_CERT`/`TLS_KEY` (uvicorn `ssl_certfile`) and
      generate a self-signed pair into `/data/tls` on first start.
- [ ] **Web UI login.** Single user: password hash in `.env`, secure/HttpOnly/SameSite session cookie,
      optional TOTP. Keep the existing same-origin check on form posts.
- [ ] Trust `X-Forwarded-*` only from the proxy (`forwarded_allow_ips`) once a proxy is in front.
- [ ] Publish an image (GHCR) so the server doesn't need to build: `image: ghcr.io/ryarmst/grocery-assist`.
- [ ] Scheduled SQLite backup (e.g. nightly `.backup` into the volume, keep 7).

## 2. MCP: make the agent experience self-contained

- [ ] **MCP prompts** for the main workflows — `weekly_shop`, `learn_from_review`, `whats_on_sale` — carrying
      the core of `skills/grocery-shopping/SKILL.md`, so any MCP client gets the workflow without
      installing the skill.
- [ ] **MCP resources** for read-mostly state: `grocery://preferences`, `grocery://runs/active`,
      `grocery://cart`, `grocery://feedback/unreviewed` (with subscriptions so a client can react when a
      run is reviewed or the cart changes).
- [ ] **OAuth on `/mcp/`** (the SDK supports an auth provider) so it can be added as a claude.ai custom
      connector / used from the Claude mobile app. Requires HTTPS reachable from the internet
      (Cloudflare Tunnel or Tailscale Funnel); keep everything except `/mcp/` behind edge auth.
- [ ] **Elicitation** for choice groups when the client supports it: the agent can ask "Kraft 1 kg or
      No Name 1 kg?" in chat; the answer is recorded as feedback. Approval (and therefore the cart)
      stays in the web UI unless a deliberate, user-confirmed path is added.
- [ ] Tool to tell the agent what changed since a run was staged (prices, stock) before the user reviews.
- [ ] Return `review_url` deep links per item (e.g. `/runs/<id>#card-<sel>`) so chat replies can link items.

## 3. Sessions and notifications

- [ ] **Session bridge** so the cart works for more than an hour: a small browser extension or bookmarklet
      that sends the nofrills.ca `AccessToken` to `/auth` (and refreshes it from inside the browser,
      where No Frills allows it). Today it's a manual paste.
- [ ] Notify when a run is ready for review or the session is about to expire (ntfy / Pushover / email).

## 4. Shopping intelligence

- [ ] Price alerts: watch items with Shopping Preferences and flag when they hit a new low.
- [ ] Multi-store: record Costco (or other store) prices manually or from receipts so "Store Preference"
      can be compared with real numbers.
- [ ] Close the loop after checkout: import the order/receipt to mark what was actually bought, feeding
      requirement cadence and Notion Groceries (`Purchased`).
- [ ] Agent runner + "Prompts" page on the server (Claude Agent SDK, only the grocery MCP and a Notion MCP
      with an integration token limited to Groceries + Pantry), with an optional weekly schedule.

## 5. Housekeeping

- [ ] Mobile layout pass on the triage and modify pages.
- [ ] CI: run `pytest` and build the image on push.
