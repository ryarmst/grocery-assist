# Skill update — 2026-11-20: deal context and meal planning

The repo copies in `skills/` are updated. If you use Claude-managed copies of these skills, apply the same
changes there:

- **grocery-shopping**
  - Start from `shopping_get_context`; use `shopping_search_catalog` for "what do we usually buy" questions,
    then confirm prices live.
  - Review reasons are now free text, and the app no longer makes rules itself, so infer preferences from
    Ryan's notes.
  - A substitution note typed during review means "substitutes OK".
  - New section: *Planning the week around deals (with meal-planner)*.
- **meal-planner**
  - New section *Deal-led planning* (before *Planning a shopping run*): read `shopping_get_context`
    with `focus: "meals"`, propose about 5 meals around the protein and produce deals, and hand the
    ingredient list to grocery-shopping.
  - Proactive mode stages a run for review and doesn't write Groceries until Ryan says go.
