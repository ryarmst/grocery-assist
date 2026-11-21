# Browser extension plan

## Stack
- **WXT** (Vite-based, Manifest V3, builds Chrome and Firefox from one codebase) with TypeScript.
- **Preact** (or Svelte) for the UI. No remote code: everything is bundled, which extension CSP requires.
- Distribution:
  - Chrome: unpacked, or a private Chrome Web Store listing for the household.
  - Firefox: an AMO-signed unlisted XPI, which installs on desktop and Android.

## Permissions (minimal)
- `storage` (settings; tokens in `storage.session`), `alarms` (inbox polling), `cookies`, `sidePanel`
  (Chrome) / `sidebar_action` (Firefox desktop).
- Host permissions: `https://*.nofrills.ca/*` (to read the session cookie) and the configured server
  origin only (added at runtime with an optional permission request).
- No `tabs`, no `<all_urls>`, and no content scripts for the main features. (Possible later: an optional
  "add to plan" button on nofrills.ca product pages.)

## Surfaces

| Surface | Contents |
|---|---|
| **Side panel / sidebar** (main) | Chat with the agent: mode chips (Shop, Meals, Pantry, Recipes, Ask), streaming replies, tool activity lines ("Searching deals in Meat…"), confirmation cards (Approve / Edit / Skip), quick replies |
| **Tab: Plan** | This week's meals as cards: deals used, uses-up, cost/serving, effort, Cook By. Buttons: Keep, Reject (+ note), Cooked (+ rating, leftovers, note), Promote. Links to the Notion page |
| **Tab: Run** | v1 triage UI rebuilt natively: approve, remove or switch, note, substitution note, quantity; approve all; live "adding to cart" status |
| **Tab: Deals** | Top deals plus "your items on sale" and best per aisle, from the context API; add to cart; dismiss with a note |
| **Tab: History** | Activity, feedback, orders, product price history, audit log |
| **Tab: Settings** | Server URL and pairing; devices (list, revoke, new pairing code); LLM providers (key entry, which is write-only, model per tier, Test); Notion token and database pickers; budgets; weekly-plan schedule on/off |
| **Popup** (toolbar) | Status at a glance: server connected, No Frills session (minutes left), inbox items. Opens the panel |
| **Badge** | Count of inbox items needing a person (plan ready, run ready, needs session, check-ins) |

On **Firefox for Android** there is no sidebar: the popup and full-tab pages take over, so every surface
also works as a tab page.

## Background service worker
- **Device key:** created at pairing; ECDSA P-256 with `extractable: false`; the `CryptoKey` lives in
  IndexedDB.
- **Access token:** obtained by signing a challenge; kept in `storage.session` (memory, cleared when the
  browser closes); refreshed when it's about to expire.
- **Session capture:**
  - `cookies.onChanged` filtered to the `.nofrills.ca` `AccessToken` cookie → `PUT /api/retailer/session`
    (debounced). Also checked at startup and on the popup's "Reconnect" button.
  - Only that one cookie is read. Nothing else from nofrills.ca is sent.
- **Inbox:** an alarm every 15 minutes (and on panel open) → `GET /api/inbox` → badge. While a run is being
  added to the cart, the panel polls faster, as the v1 page does.
- **No secrets at rest in the extension:** provider and Notion keys go straight from the Settings form to
  the server and are never written to extension storage. The only long-lived secret is the device
  private key, which can't be exported.

## Pairing flow
1. Settings → enter server URL → the extension requests host permission for that origin.
2. Enter the one-time code (from `grocery pair` on the server, or Settings → "New pairing code" on an
   already-paired device).
3. Generate the key pair → `POST /api/pair` → success shows the device name. On failure, the code is
   unchanged and three attempts are allowed before it's burned.

## Optional mTLS
On desktop, Caddy can additionally require a per-device client certificate imported into the browser or
OS store.
- **Chrome:** the extension service worker's requests need the certificate auto-selected via the
  `AutoSelectCertificateForUrls` policy, or the request fails silently. Spike S4.
- **Firefox desktop:** prompts once per session.
- **Firefox Android:** can't import client certificates. Those devices rely on device keys alone, so mTLS
  can't be the only lock if Android is in scope.

## Chat UX details
- Streaming via `fetch` and a `ReadableStream` parsing SSE. A **Stop** button cancels the request and the
  server stops the loop.
- Tool activity appears as small grey lines; expanding one shows its arguments and result summary.
- Confirmation cards show exactly what will be written (for example "Pantry → Chicken Thighs:
  Expiration = Nov 27"). Approve, Edit (opens a small form) or Skip.
- Each answer shows the model and cost ("Gemini Flash · free" / "Sonnet · $0.012").
- Threads are kept on the server so they're shared across devices. The panel shows the last 10.
