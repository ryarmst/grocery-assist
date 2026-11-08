# Shopping Assistant (No Frills / PC Express)

A local app that lets an AI agent build a grocery order for you to review:

- **MCP server** (`/mcp/`) — search products, inspect deals, read preferences and price
  history, stage shopping runs, and (after you approve) add items to your No Frills cart.
- **Web UI** — review runs, pick alternatives, approve/reject, record preferences, view the
  cart, price history, and connection status.
- **SQLite history** — every product the agent considers gets a price observation (price,
  package size, normalized unit price, deal), so "is this actually a good price?" and
  "did the package shrink?" can be answered over time.

**It cannot buy anything.** There is no checkout, order, payment or timeslot code, the
retailer client refuses any endpoint outside a small allow-list, and the MCP surface has no
generic HTTP tool. You complete purchases yourself on nofrills.ca.

See [`docs/api-research.md`](docs/api-research.md) for how the retailer API works.

## Quick start (Docker)

```bash
git clone <this repo> shopping-assistant && cd shopping-assistant
cp .env.example .env        # then set PCX_API_KEY (see the comment in the file)
docker compose up -d
```

Open <http://localhost:8000>, go to **Auth**, and paste a session token (below).
Data lives in `./data` (`app.db`, `session/`).

## Quick start (without Docker)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env        # set PCX_API_KEY
.venv/bin/shopping-assistant
```

## Connecting your No Frills session

Search and deals work without logging in. The cart needs your session. No Frills' login
and token-refresh endpoints block non-browser clients, so the app can't log in by itself;
instead you paste the token your browser already has:

1. Log in at nofrills.ca (reload the page if it's been open a while).
2. DevTools → **Application** (Firefox: **Storage**) → **Cookies** → `https://www.nofrills.ca`.
3. Copy the `AccessToken` cookie value (starts with `eyJ`) into <http://localhost:8000/auth>.

The token lasts about an hour. The header shows the time remaining; cart actions fail with a
clear "paste a fresh AccessToken" message when it has expired. The token is stored in
`data/session/token.json` (mode 600) and never exposed through MCP or logs.

## Connecting the agent

MCP endpoint (streamable HTTP): `http://localhost:8000/mcp/` (note the trailing slash).

Claude Code:

```bash
claude mcp add --transport http shopping http://localhost:8000/mcp/
```

If you set `MCP_TOKEN`, add `--header "Authorization: Bearer <token>"`.

Give the agent the skill in [`skills/grocery-shopping/SKILL.md`](skills/grocery-shopping/SKILL.md);
it also needs your Notion connector for the grocery list and pantry.

Check everything end to end (creates a small test run, doesn't touch the cart):

```bash
.venv/bin/python scripts/mcp_smoke.py
```

### MCP tools

| Tool | Purpose |
|---|---|
| `shopping_search_products` | Search; normalized prices, unit prices, deals, preference flags, our history |
| `shopping_get_product` | One product with history stats |
| `shopping_get_deals` | Flyer items or price reductions, optional query / category / min discount |
| `shopping_get_cart` | Current cart |
| `shopping_create_run` / `shopping_get_run` / `shopping_list_runs` | Shopping runs |
| `shopping_add_selection` / `shopping_add_choice_group` / `shopping_update_selection` | Stage items (with reasons + rationale) |
| `shopping_submit_run_for_review` | Hand the run to you; returns the review URL |
| `shopping_get_preferences` / `shopping_add_preference_note` | Preferences |
| `shopping_get_product_history` / `shopping_get_selection_history` / `shopping_get_recent_price_history` | History |
| `shopping_get_feedback` | What you removed/swapped during review and why, plus detected patterns (`unreviewed_only` for new records) |
| `shopping_mark_feedback_reviewed` | Mark feedback as handled (e.g. after the skill saves it to Notion's Shopping Preferences) |
| `shopping_get_requirement_history` | Per item: last bought, typical interval, recently skipped, due / not due (for regular purchases) |
| `shopping_apply_approved` | Add **user-approved** items to the cart |
| `shopping_auth_status` | Whether the retailer session is connected (no secrets) |

## Review workflow

The agent creates a run and stages items → you open the review URL. Items are grouped into
*From the grocery list*, *Regular purchases* and *Deals & extras*, with filter tabs for Pending / In cart /
Removed. Either work down the page or press **Triage** to go through pending items one at a time.

For each item: pick an option (for choice groups) and **Approve**, or **Remove**. **Approving puts the
item in your cart immediately** (when your session is connected); **Approve all & add to cart** does the
same for every pending item on screen — items the agent adds after you loaded the page stay pending.
Open **Why?** to add reason chips (“have enough”, “wrong size”, “better unit price”…) and a note, and
optionally turn the decision into a rule. Cart quantities are only ever raised to the approved amount,
never double-added on retry; if No Frills has an outage the item shows *Cart error* and an
**Add N approved to cart** button retries.

### Modify

Every cart line and every decided run item has **Modify…**: one page to change the quantity, swap to
another product (the agent's other options plus a live search sorted by unit price), remove it, or just
leave a note — each with optional reasons and a rule. Removed run items can be **added back**.

### History

*History* has four tabs: **Activity** (one timeline of suggestions, approvals, removals, swaps, cart
changes with their source, notes and rules — filterable by type, item, period or run), **Products &
prices**, **Feedback & patterns** and **Rules**. Each product page shows its observed prices and
everything that happened with it.

### Feedback

*History → Feedback & patterns* collects every remove/swap with its reasons. It counts reasons, flags repeated patterns
(e.g. the same product removed twice as disliked → suggest a blacklist; the same alternative picked
twice → suggest "use this for <item>"; an item removed repeatedly as "have enough" → suggested too
often) with a one-click **Create this rule**, and lets you mark records reviewed with a note on what you
did (rule, skill change, app change). The agent reads the same data via `shopping_get_feedback`, and
search results carry per-product `user_feedback`.

### Regular purchases

Notion stays the source of truth for *what* is a regular purchase (`Regular Purchase?` in Kitchen
Pantry and Storage). The skill passes those names to `shopping_get_requirement_history`, which uses the
app's own history (last added to cart, typical interval, last “have enough” skip, preferred product) to
say which are due. This replaces a Notion automation that copies regular items onto the grocery list.
Use the exact Notion item name as the selection's `requirement` so history lines up.

## Configuration

See `.env.example`. Key settings: `PCX_API_KEY`, `STORE_ID` (default 3728),
`HOST` (one or more comma-separated addresses), `PUBLIC_URL`, `ALLOWED_HOSTS`, `MCP_TOKEN`, `LOG_LEVEL`.

Example — reachable from the VM itself and from the host over a host-only adapter, but not on other
interfaces:

```
HOST=127.0.0.1,192.168.208.128
ALLOWED_HOSTS=localhost,127.0.0.1,192.168.208.128
PUBLIC_URL=http://192.168.208.128:8000
```

**Network exposure:** by default everything binds to localhost. The web UI has no login of
its own, so if you expose it beyond localhost put it behind something that authenticates
you (VPN, reverse proxy with auth), set `MCP_TOKEN`, and add your hostname to `ALLOWED_HOSTS`.

## Development

```bash
.venv/bin/python -m pytest              # unit tests (fixtures, no network)
```

- `src/shopping/retailer/` — `client.py` (allow-listed API calls), `normalize.py` (sizes, unit prices, deals)
- `src/shopping/auth/session.py` — `AuthSession` (manual token today; a browser-assisted refresh can slot in later)
- `src/shopping/core/service.py` — runs, selections, approvals, preferences, history
- `src/shopping/mcp_server/server.py` — MCP tools
- `src/shopping/web/` — UI routes and templates
- `src/shopping/migrations/` — Alembic (applied automatically at startup).
  New migration: `python -c "from shopping.database import alembic_config; from alembic import command; command.revision(alembic_config(), message='...', autogenerate=True)"`
- `scripts/sanitize_fixture.py` — turn a captured response into a fixture without personal data or tokens.

Logs redact `Authorization`, cookies, API keys, JWTs and token/password fields at every
level, including DEBUG.

## Never commit

`user.txt`, `.env`, `*.har`, `data/` — all in `.gitignore`.
