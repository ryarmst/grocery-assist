# grocery-assist

A small self-hosted service that lets an AI agent shop at No Frills for you — without being able to buy
anything.

- **For the agent:** an MCP server (`/mcp/`) to search products and deals, read price history and your
  preferences, and stage a *shopping run* with reasons for every item.
- **For you:** a web UI to triage the run. Approving an item puts it in your No Frills cart; removing
  or swapping it records why, so the agent learns.
- **Top deals:** standout prices the agent spots go on a Deals page; add one to the cart in a tap or
  dismiss it with a reason.
- **No checkout.** There is no code to place orders, book timeslots or pay. You check out on nofrills.ca.

## Deploy (Docker)

```bash
git clone git@github.com:ryarmst/grocery-assist.git && cd grocery-assist
cp .env.example .env      # set PCX_API_KEY, PUBLIC_URL, MCP_TOKEN (see comments in the file)
docker compose up -d
```

Open `PUBLIC_URL`, go to **Auth**, and connect your No Frills session (below). That's it.

- Data (SQLite + session) lives in the `grocery-data` volume.
- Update: `git pull && docker compose up -d --build`
- Logs: `docker compose logs -f`
- Backup: `docker run --rm -v grocery-assist_grocery-data:/data -v "$PWD":/out alpine tar czf /out/grocery-data.tgz -C /data .`
- Footprint: ~90 MB RAM, ~0% CPU when idle; capped at 256 MB / 0.5 CPU; read-only, non-root container.

### Connect your No Frills session

Searching and deals need no login; the cart does. No Frills blocks automated logins, so paste the
token your browser already has: log in at nofrills.ca → DevTools → Application/Storage → Cookies →
`https://www.nofrills.ca` → copy `AccessToken` → paste on the app's **Auth** page. It lasts about an hour;
the header shows the time left.

### Security notes

- The web UI has **no login yet** — keep it on your home network or behind a VPN (see TODO.md).
- The MCP endpoint requires `Authorization: Bearer <MCP_TOKEN>`.
- Serve it over HTTPS with a reverse proxy for now (built-in TLS is on the TODO list).

## Connect an LLM (MCP)

Endpoint: `<PUBLIC_URL>/mcp/` (streamable HTTP, note the trailing slash), header
`Authorization: Bearer <MCP_TOKEN>`.

Claude Code:

```bash
claude mcp add --transport http grocery http://<host>:8000/mcp/ --header "Authorization: Bearer <MCP_TOKEN>"
```

Other MCP clients use the same URL and header. Give the agent the skill in
[`skills/grocery-shopping/SKILL.md`](skills/grocery-shopping/SKILL.md) and a Notion connection (grocery list
and pantry live there). Its companion, [`skills/meal-planner`](skills/meal-planner/SKILL.md), plans meals,
keeps the Notion pantry current and writes the Groceries list this app shops from.

### How the agent uses it

1. Read preferences and past feedback → `shopping_get_preferences`, `shopping_get_feedback`
2. Find products and deals → `shopping_search_products`, `shopping_get_deals`, `shopping_get_product`
3. Check history → `shopping_get_product_history`, `shopping_get_requirement_history` (is a regular item due?)
4. Stage a run → `shopping_create_run`, `shopping_add_selection`, `shopping_add_choice_group` (several options,
   one recommended), `shopping_update_selection`, `shopping_submit_run_for_review` → returns a review URL.
   Each item carries a substitution preference (off by default; the skill allows it only where a swap is
   harmless)
   Standout prices on things not needed this week → `shopping_add_top_deal` (the Deals page);
   `shopping_get_top_deals` shows what you added or dismissed, and why; `shopping_retire_top_deal`
5. **You** triage it in the web UI; approved items go into the cart
6. Learn from your review → `shopping_get_feedback`, then `shopping_mark_feedback_reviewed` once the lesson
   is saved (the skill writes it to Notion's Shopping Preferences)

Also: `shopping_get_past_orders` / `shopping_get_past_order` (what was actually bought: items, prices paid,
which run suggested each item, what was dropped at checkout — importing an order also feeds price and
purchase history), `shopping_get_cart`, `shopping_list_runs`, `shopping_get_run`, `shopping_get_selection_history`,
`shopping_get_recent_price_history`, `shopping_add_preference_note`, `shopping_auth_status`, and
`shopping_apply_approved` (retries adding *already approved* items, e.g. after a session expired).

Check a deployment end to end (creates a small test run, never touches the cart):

```bash
MCP_TOKEN=<token> python scripts/mcp_smoke.py http://<host>:8000/mcp/
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env && .venv/bin/shopping-assistant     # http://localhost:8000
.venv/bin/python -m pytest
```

How the No Frills API works: [`docs/api-research.md`](docs/api-research.md). Next steps: [`TODO.md`](TODO.md).
Never commit `.env`, `user.txt`, `*.har` or `data/` (all gitignored).
