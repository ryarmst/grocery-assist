"""One chronological feed of everything that happened: suggestions, approvals, removals, swaps,
cart changes, notes and rules. Built from the existing tables, newest first."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shopping.core.feedback import feedback_dict
from shopping.core.service import product_label
from shopping.models import PREFERENCE_KINDS, CartAction, Feedback, Preference, Product, Selection, utcnow

KINDS = {
    "suggested": "Suggested",
    "approved": "Approved",
    "removed": "Removed",
    "swapped": "Swapped",
    "cart": "Cart change",
    "note": "Note",
    "rule": "Rule saved",
}


def activity(session: Session, *, days: int = 30, kind: str | None = None, q: str | None = None,
             product_id: str | None = None, run_id: str | None = None, limit: int = 300) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    events: list[dict] = []

    sel_q = select(Selection).where(Selection.created_at >= since)
    if run_id:
        sel_q = sel_q.where(Selection.shopping_run_id == run_id)
    removal_reasons: dict[str, Feedback] = {}
    fb_q = select(Feedback).where(Feedback.created_at >= since)
    if run_id:
        fb_q = fb_q.where(Feedback.shopping_run_id == run_id)
    feedback_rows = list(session.scalars(fb_q))
    for f in feedback_rows:
        if f.action == "removed" and f.selection_id:
            removal_reasons[f.selection_id] = f

    for s in session.scalars(sel_q):
        base = {"product": product_label(s.product), "product_id": s.product.retailer_product_id, "run_id": s.shopping_run_id,
                "run": s.run.summary, "requirement": s.requirement}
        price = s.observation.effective_price if s.observation else None
        # Alternatives offered alongside a recommendation are part of that suggestion, not separate ones.
        is_alternative = s.choice_group is not None and s.choice_group.recommended_selection_id != s.id and s.added_by != "user"
        if not is_alternative:
            n_alt = len(s.choice_group.selections) - 1 if s.choice_group is not None else 0
            events.append({**base, "time": s.created_at, "kind": "suggested",
                           "title": "You added" if s.added_by == "user" else "Agent suggested",
                           "detail": f"×{s.suggested_quantity or s.quantity:g}" + (f" at ${price:.2f}" if price is not None else "")
                                     + (f" · with {n_alt} alternative{'s' if n_alt != 1 else ''}" if n_alt else ""),
                           "reasons": s.reason_codes, "note": s.rationale})
        if s.approved_at:
            qty = f"×{s.quantity:g}" + (f" (suggested ×{s.suggested_quantity:g})" if s.suggested_quantity and s.suggested_quantity != s.quantity else "")
            events.append({**base, "time": s.approved_at, "kind": "approved", "title": "Approved", "detail": qty})
        # Unchosen options of a choice group are "not picked", not removals; skip them unless the user gave a reason.
        not_picked = s.choice_group is not None and s.choice_group.selected_selection_id != s.id and s.id not in removal_reasons
        if s.rejected_at and not not_picked:
            f = removal_reasons.get(s.id)
            events.append({**base, "time": s.rejected_at, "kind": "removed", "title": "Removed from run",
                           "detail": ", ".join(feedback_dict(f)["tag_labels"]) if f else "",
                           "note": f.note if f else (s.last_error if s.last_error == "withdrawn by agent" else None)})

    for f in feedback_rows:
        d = feedback_dict(f)
        base = {"time": f.created_at, "run_id": f.shopping_run_id, "requirement": f.requirement, "product": d["product"], "product_id": d["product_id"]}
        if f.action == "chose_alternative":
            events.append({**base, "kind": "swapped", "title": "Swapped", "detail": f"→ {d['chosen_product']}",
                           "chosen_product_id": d["chosen_product_id"], "reasons_text": ", ".join(d["tag_labels"]), "note": f.note})
        elif f.action in ("note", "approved", "quantity_changed") and (f.note or f.tags):
            events.append({**base, "kind": "note", "title": {"note": "Note", "approved": "Kept — note", "quantity_changed": "Quantity changed — note"}[f.action],
                           "detail": ", ".join(d["tag_labels"]), "note": f.note})

    ca_q = select(CartAction).where(CartAction.timestamp >= since)
    if run_id:
        ca_q = ca_q.where(CartAction.shopping_run_id == run_id)
    for a in session.scalars(ca_q):
        before, after = a.quantity_before or 0, a.quantity_after or 0
        verb = "Added to cart" if before == 0 and after > 0 else "Removed from cart" if after == 0 else "Cart quantity changed"
        label = a.product_label or (product_label(session.get(Product, a.product_id)) if a.product_id else None) or a.retailer_product_id
        events.append({"time": a.timestamp, "kind": "cart", "title": verb if a.success else f"{verb} — failed", "ok": a.success,
                       "product": label, "product_id": a.retailer_product_id, "run_id": a.shopping_run_id,
                       "detail": f"{before:g} → {after:g}" + (f" at ${a.price_at_action:.2f}" if a.price_at_action is not None else "")
                                 + (f" · via {a.source.replace('_', ' ')}" if a.source else ""),
                       "note": a.error})

    for p in session.scalars(select(Preference).where(Preference.created_at >= since)):
        if run_id:
            continue
        events.append({"time": p.created_at, "kind": "rule", "title": "Rule saved", "product": p.target_label or p.target,
                       "product_id": p.target if p.target and "_" in p.target else None,
                       "detail": PREFERENCE_KINDS.get(p.kind, p.kind) + (f" (for {p.scope})" if p.scope else "") + ("" if p.active else " · inactive"),
                       "note": p.note})

    if kind:
        events = [e for e in events if e["kind"] == kind]
    if product_id:
        events = [e for e in events if product_id in (e.get("product_id"), e.get("chosen_product_id"))]
    if q:
        needle = q.lower()
        events = [e for e in events if needle in " ".join(str(e.get(k) or "") for k in ("product", "requirement", "note", "detail")).lower()]
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[:limit]
