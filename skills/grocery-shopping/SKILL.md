---
name: grocery-shopping
description: Build a No Frills grocery order for review. Reads the grocery list and pantry from Notion, uses the shopping-assistant MCP tools to search products, check deals and price history, applies saved preferences, and stages a shopping run the user approves in the web UI. Use when the user asks to go grocery shopping, fill the cart, restock, plan the weekly order, or check what's on sale at No Frills.
---

# Grocery shopping

You prepare a shopping run; the user approves it. You can never check out — the user
completes the purchase on nofrills.ca themselves.

## Sources of truth

- **Notion** holds the household state: the *Groceries* list (what's needed) and *Kitchen
  Pantry and Storage* (what's on hand, quantities, expiry). Read them; don't ask the user to
  repeat what's there. The shopping app does not read Notion.
- **The shopping-assistant MCP server** holds shopping state: products, prices, history,
  preferences, runs, and the cart.

## Workflow

1. **Gather needs.** Read the Groceries list. Check the pantry for staples that are low or
   out, and items expiring soon that need replacing. Note anything the user asked for in chat.
2. **Load preferences:** `shopping_get_preferences`. Blacklists (`absolute: true`) are hard
   rules. Product/brand preferences and notes are strong guidance, not absolutes — a much
   better deal can justify an alternative, but say so.
3. **Look for deals:** `shopping_get_deals` (kind `flyer`, optionally `price_reduction`) once,
   broadly, and again with `query` for the main categories you need.
4. **Search each requirement:** `shopping_search_products`. Use `sort_by_unit_price` for
   commodity items. Compare **unit price** (per 100 g / 100 mL / item) within the same
   comparison family — never weight against volume. For `each_priced_by_weight` items the
   shelf price is an estimate for an average pack.
5. **Judge deals with history, not just the sale tag.** A result's `history` (when present)
   shows the lowest price seen, the 90-day average and `vs_avg_90d_pct`. Call a price a good
   deal when it's at or near the lowest we've seen, or well below the average — not just
   because the retailer says SALE. `package_size_changed` means compare unit prices; a
   same-price smaller package is a price increase.
6. **Check what we usually buy:** `shopping_get_selection_history` (query by item) shows
   past choices, typical quantity and what the user rejected.
7. **Create the run:** `shopping_create_run` with a short summary ("Weekly groceries — Nov 3").
8. **Stage items.**
   - Clear choice → `shopping_add_selection`.
   - Real trade-off (brand vs. price, size vs. unit price, sale vs. preferred) →
     `shopping_add_choice_group` with 2–4 options and your recommendation first or marked by
     `recommended_index`.
   - Always give `reasons` (any that apply): `grocery_list`, `pantry_low`, `regular_staple`,
     `good_deal`, `preferred_product`, `preferred_brand`, `user_requested`, `alternative`.
   - Always give a one- or two-sentence `rationale` with the concrete numbers
     ("4 L at $0.16/100 mL, $0.25 below regular; lowest we've seen").
   - Set `requirement` to the need being filled ("milk", "peanut butter").
   - Quantity: what the list says; otherwise the typical quantity from history; respect
     `max_quantity`. Don't overbuy perishables because of a sale.
   - Optional opportunistic deals on regular staples are fine (reason `good_deal` +
     `regular_staple`), but keep them few and clearly explained.
9. **Submit:** `shopping_submit_run_for_review`. Give the user the `review_url`, the estimated
   total, and a short summary: what's included, notable deals, anything you couldn't find,
   and any choice groups waiting on them.
10. **Applying to the cart** happens when the user clicks *Apply approved to cart* in the web
    UI. Only call `shopping_apply_approved` if the user explicitly asks you to, and only after
    they've approved items. If it fails with an authentication error, ask them to paste a fresh
    token at the `/auth` page (`shopping_auth_status` gives the URL).

## Preferences the user states in chat

If the user tells you a lasting preference ("we only buy whole-bean coffee unless it's a huge
sale"), record it with `shopping_add_preference_note` (scope = item type). Product/brand
likes and blacklists are best recorded by the user from the review page.

## Don'ts

- Don't try to check out, book timeslots, or change payment — no tool exists for it, and you
  shouldn't ask the user for credentials or tokens in chat.
- Don't stage blacklisted products unless the user explicitly asked for that exact item
  (then include `user_requested`).
- Don't update Notion's pantry as if items were bought — they're only bought when the user
  checks out. Offer to update the pantry after the user confirms the order.
