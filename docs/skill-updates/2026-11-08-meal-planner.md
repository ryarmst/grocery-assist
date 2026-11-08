# Update request: `meal-planner` skill

Please update my `meal-planner` skill (SKILL.md) with the changes below. Keep everything not mentioned —
inventory handling, receipts, trust model, recipe flow and tiers, regular recipes and staging, the chat
output template, and `references/*.md` — exactly as it is.

Summary: the Kitchen Pantry and Storage database has two new properties, **Shopping Preferences** and
**Store Preference**. The grocery-shopping skill fills Shopping Preferences from my No Frills cart reviews
(removals, swaps, notes). Meal-planner should read both, respect them when planning shopping, keep the
shared format when it writes, and use Groceries `Quantity` consistently with them.

---

## 1. "Kitchen Pantry and Storage" property table

Add these two rows after `Regular Purchase?`:

| Property | Type | Meaning |
|---|---|---|
| `Shopping Preferences` | text | Durable buying knowledge for the ingredient: brand, variety, size, quality and price conditions, learned from Ryan's shopping reviews (written by the grocery-shopping skill) and from what he says. Shared format below. Read it whenever shopping or quantities are involved. |
| `Store Preference` | select | Where Ryan usually gets the best deal: Grocery Store, Costco. Empty means Grocery Store. A soft preference about deals, not a rule. |

Replace the `Regular Purchase?` meaning with:

> Bought weekly or bi-weekly. Checked items are added by default to full shopping lists (see Regular recipes and shopping); the grocery-shopping skill and the shopping app track how often they're actually bought. Never change it unless he says.

Add directly under the table:

> **Shopping Preferences format** (shared with the grocery-shopping skill): one fact per line, newest last, at most ~6 lines, each starting with `Prefer:` (brand/product/variety), `Avoid:` (don't buy — firm), `Size:` (package or format), `Buy when:` (conditions, price thresholds) or `Note:`. A line may end with a month tag like `[2026-11]`. Newer lines win over contradicting older ones. Ryan may also write free text without a prefix ("2% bags", "Bulk, good prices preferred"): read it the same way and keep his lines verbatim when merging. Example:
>
> ```
> Prefer: sourdough or Italian loaf
> Avoid: packaged multigrain sandwich loaves
> Size: 10 lb bag
> ```

## 2. "Other databases" → Groceries

Append to the Groceries bullet:

> `Quantity` is what the grocery-shopping skill sizes the cart from, so make it specific and purchasable: a count, weight or package ("2", "3 lb (1.4 kg)", "1 bag", "2 dozen"), following the row's `Size:` preference when there is one. Groceries has no store field: items whose pantry `Store Preference` is Costco still go on the list; the grocery-shopping skill buys them at No Frills only on a genuinely good deal and otherwise sets them aside for Costco.

## 3. "Prompting and writes" → Field rules

Add these bullets:

> - **Buying preferences stated in chat** ("we only buy sourdough", "get chicken at Costco"): propose the Shopping Preferences line (format above) and/or the `Store Preference` change in the one-line Notion proposal; write only on confirmation. Write the full merged field: keep existing lines, drop duplicates, replace a line the new one contradicts. Never rewrite lines for style.
> - **Store Preference**: set only when Ryan says where he buys something, or confirms a proposal.

Change the last "Never …" bullet to:

> - Never delete rows, including expired One-Off rows. Never press buttons. Never change `Replace?`, `Regular Purchase?`, `Regular Recipe?` or `Store Preference` unless he says; change `Shopping Preferences` only as described above.

## 4. "New items"

Append to the rows-created paragraph:

> If he says where he buys it (e.g. "Costco"), set `Store Preference` too.

## 5. "Receipts" → step 4 (Plan the writes)

Add a bullet:

> - Receipt from Costco: for matched rows whose `Store Preference` is empty, propose `Store Preference` → Costco in an extras line ("Store: set Costco for …?"). Apply only on confirmation. Never change `Shopping Preferences` from a receipt.

And add "Store: …" to the list of optional one-line extras in step 5.

## 6. "Planning a shopping run"

- **Step 4 (Consolidate aggressively)** — append:
  > Respect each row's Shopping Preferences: never swap toward something an `Avoid:` line excludes, and prefer the `Prefer:` choice when consolidating.
- **Step 7 (Merge and quantify)** — append:
  > Round to the format a `Size:` line names (e.g. "Size: 10 lb bag" → `1 bag (10 lb)`).
- **Step 9 (Report)** — add a `Store` column to the table, filled only for Costco items, and add the optional line:
  > "Costco: …" for listed items whose `Store Preference` is Costco.

## 7. "Building recipes" → Missing ingredients

Append:

> When suggesting an optional add, honour its row's Shopping Preferences (e.g. don't suggest a variety marked `Avoid:`).
