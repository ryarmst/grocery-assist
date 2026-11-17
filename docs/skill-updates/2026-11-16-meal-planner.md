# Update request: `meal-planner` skill (shopping contract)

Please update my `meal-planner` skill. Only `references/shopping-contract.md` changes (plus one line in
SKILL.md); keep everything else exactly as it is.

Summary: the grocery app now supports per-item **substitutions**, and the grocery-shopping skill records
Ryan's substitution choices in Shopping Preferences with a new `Subs:` line. Meal-planner shares that
field, so it needs to know the prefix and keep those lines when merging.

## 1. `references/shopping-contract.md` — Shopping Preferences format

Add `Subs:` to the list of line prefixes, with this meaning:

> `Subs:` — whether the shopper may substitute this item if it's unavailable, and how. Examples:
> `Subs: never` · `Subs: OK — any brand, same size` · `Subs: OK — 2% 4 L bags only`.
> Written by the grocery-shopping skill from Ryan's substitution choices and order outcomes, or from what
> he says in chat. Meal-planner reads it but only writes it when Ryan states a substitution preference in
> conversation ("never sub the coffee"), via the usual one-line Notion proposal.

Add to the merge rules:

> Keep a single `Subs:` line per item; a newer one replaces the older one.

## 2. SKILL.md — "Prompting and writes" → Field rules

In the **Buying preferences stated in chat** bullet, add substitutions to the examples:

> ("we only buy sourdough", "get chicken at Costco", "never substitute the coffee")
