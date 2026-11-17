"""Server-rendered web UI: runs and triage, cart with per-item Modify, and a History area
(activity timeline, prices, feedback, rules) plus auth status."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware

from shopping import context
from shopping.auth.session import AuthRequired
from shopping.config import settings
from shopping.core import activity as act
from shopping.core import feedback as fbk
from shopping.core import modify as mod
from shopping.core import orders as ords
from shopping.core import service
from shopping.database import session_scope
from shopping.models import (
    FEEDBACK_TAG_LABELS,
    FEEDBACK_TAGS,
    PREFERENCE_KINDS,
    REASON_CODES,
    Feedback,
    PastOrder,
    Preference,
    PriceObservation,
    Product,
    Selection,
    ShoppingRun,
)
from shopping.retailer.client import RetailerError

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS_LABELS = {"pending": "Pending", "approved": "Approved · not in cart", "applied": "In cart", "rejected": "Removed",
                 "failed": "Cart error", "review": "In review", "building": "Building", "partially_approved": "Partly approved",
                 "cancelled": "Cancelled"}
RUN_FILTERS = [("all", "All"), ("pending", "Pending"), ("applied", "In cart"), ("approved", "Approved, not in cart"),
               ("rejected", "Removed"), ("failed", "Cart errors")]


# -- template helpers -------------------------------------------------------------------

def _money(v) -> str:
    return "—" if v is None else f"${float(v):,.2f}"


def _unit(price, basis) -> str:
    if price is None or not basis:
        return ""
    label = {"100g": "100 g", "100ml": "100 mL", "1ea": "item"}.get(basis, basis)
    p = float(price)
    return f"${p:.2f} / {label}" if p >= 0.1 or basis == "1ea" else f"{p * 100:.1f}¢ / {label}"


def _local(dt) -> str:
    return "" if dt is None else dt.astimezone(service.LOCAL_TZ).strftime("%b %-d, %Y %-I:%M %p")


def _qty(q) -> str:
    return "" if q is None else (str(int(q)) if float(q).is_integer() else f"{q:g}")


templates.env.filters.update(money=_money, local=_local, qty=_qty, status_label=lambda s: STATUS_LABELS.get(s, s))
templates.env.globals.update(unit_price=_unit, REASON_CODES=REASON_CODES, PREFERENCE_KINDS=PREFERENCE_KINDS, FEEDBACK_TAGS=FEEDBACK_TAGS,
                             FEEDBACK_TAG_LABELS=FEEDBACK_TAG_LABELS, ACTIVITY_KINDS=act.KINDS, store_id=settings.store_id)


def _back(url: str, msg: str | None = None, err: str | None = None, anchor: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
        sep = "&"
    if err:
        url += f"{sep}err={quote(err)}"
    if anchor:
        url += f"#{anchor}"
    return RedirectResponse(url, status_code=303)


class SameOriginPostMiddleware(BaseHTTPMiddleware):
    """Reject cross-site form posts (CSRF) to the UI. MCP has its own auth/host checks."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and not request.url.path.startswith("/mcp"):
            source = request.headers.get("origin") or request.headers.get("referer")
            host = request.headers.get("host", "")
            if not source or urlsplit(source).netloc != host:
                return HTMLResponse("Cross-origin form submission refused.", status_code=403)
        return await call_next(request)


def _auth_ctx() -> dict:
    return {"auth": context.auth.status()}


def _connected() -> bool:
    return context.auth.status().state == "connected"


async def _add_to_cart(session, run_id: str, sel_ids: list[str] | None) -> tuple[str | None, str | None]:
    """After approving: put the approved items in the cart right away. Returns (msg, err)."""
    if not _connected():
        return "Approved. Connect your No Frills session (Auth) to add approved items to the cart.", None
    try:
        result = await service.apply_approved(session, context.client(), run_id, only_ids=sel_ids, source="approve")
    except (AuthRequired, RetailerError) as exc:
        return None, f"Approved, but not added to the cart: {exc}"
    if result.get("message"):
        return None, None
    msg = f"Added {result['applied']} item(s) to the cart." if result["applied"] else None
    err = f"Approved, but {result['failed']} item(s) couldn't be added: {result['error']}" if result["failed"] else None
    return msg, err


# -- runs ---------------------------------------------------------------------------------

def _run_counts(run) -> dict:
    counts: dict[str, int] = {}
    for s in service.chosen_selections(run):
        counts[s.status] = counts.get(s.status, 0) + 1
    return counts


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    with session_scope() as session:
        runs = list(session.scalars(select(ShoppingRun).order_by(ShoppingRun.created_at.desc()).limit(30)))
        rows = [{"run": r, "counts": _run_counts(r), "item_count": len(service.chosen_selections(r)),
                 "total": service.estimated_total(r)} for r in runs]
        return templates.TemplateResponse(request, "index.html", {"rows": rows, **_auth_ctx()})


def _cards(session, run) -> list[dict]:
    """One card per requirement: a standalone selection, or a choice group with its options."""
    prefs = service.active_preferences(session)
    products = [s.product.retailer_product_id for s in run.selections]
    fb_hist = fbk.feedback_by_product(session, products)
    run_feedback: dict[str, list[dict]] = {}
    for f in session.scalars(select(Feedback).where(Feedback.shopping_run_id == run.id).order_by(Feedback.created_at)):
        run_feedback.setdefault(f.selection_id, []).append(fbk.feedback_dict(f))
    reqs = {r["requirement"].lower(): r for r in fbk.requirement_history(session, sorted({s.requirement for s in run.selections if s.requirement}))}

    def card(sel):
        obs = sel.observation
        # Feedback from earlier runs only (not the decision just made in this run).
        hist = fb_hist.get(sel.product.retailer_product_id)
        if hist and run_feedback.get(sel.id):
            hist = None
        req = reqs.get((sel.requirement or "").lower())
        if req and req["last_added"] is None and req["last_removed"] is None:
            req = None
        return {"sel": sel, "p": sel.product, "obs": obs, "req": req, "fb": hist,
                "stats": service.price_stats(session, sel.product, store_id=obs.store_id if obs else None),
                "flags": service.preference_flags(prefs, sel.product)}

    cards = []
    for sel in run.selections:
        if sel.choice_group_id is None:
            cards.append({"kind": "single", "main": card(sel), "options": [], "created": sel.created_at,
                          "feedback": run_feedback.get(sel.id, [])})
    for g in run.choice_groups:
        chosen = next((s for s in g.selections if s.id == g.selected_selection_id), g.selections[0])
        cards.append({"kind": "group", "group": g, "main": card(chosen), "options": [card(s) for s in g.selections],
                      "created": g.created_at, "feedback": [f for s in g.selections for f in run_feedback.get(s.id, [])]})
    cards.sort(key=lambda c: c["created"])
    return cards


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str, show: str = "all"):
    with session_scope() as session:
        try:
            run = service.get_run(session, run_id)
        except service.ShoppingError as exc:
            return HTMLResponse(str(exc), status_code=404)
        cards = _cards(session, run)
        counts = _run_counts(run)
        visible = cards if show == "all" else [c for c in cards if c["main"]["sel"].status == show]
        sections = [(key, title, [c for c in visible if fbk.section_of(c["main"]["sel"]) == key]) for key, title in fbk.SECTIONS]
        snapshot = [c["main"]["sel"].id for c in cards if c["main"]["sel"].status == "pending"]
        return templates.TemplateResponse(request, "run.html", {
            "run": run, "sections": sections, "cards": visible, "snapshot": ",".join(snapshot), "counts": counts,
            "show": show, "filters": RUN_FILTERS, "total": service.estimated_total(run),
            "editable": run.status != "cancelled", **_auth_ctx(),
        })


@router.get("/runs/{run_id}/triage", response_class=HTMLResponse)
def triage_page(request: Request, run_id: str, skipped: str = ""):
    with session_scope() as session:
        try:
            run = service.get_run(session, run_id)
        except service.ShoppingError as exc:
            return HTMLResponse(str(exc), status_code=404)
        cards = _cards(session, run)
        skip_ids = {x for x in skipped.split(",") if x}
        order = {k: i for i, (k, _) in enumerate(fbk.SECTIONS)}
        pending = [c for c in cards if c["main"]["sel"].status == "pending"]
        pending.sort(key=lambda c: (order[fbk.section_of(c["main"]["sel"])], c["created"]))
        todo = [c for c in pending if c["main"]["sel"].id not in skip_ids] or pending  # all skipped: go round again
        if not todo or run.status == "cancelled":
            return _back(f"/runs/{run_id}", msg="All items decided.")
        card = todo[0]
        return templates.TemplateResponse(request, "triage.html", {
            "run": run, "card": card, "done": len(cards) - len(pending), "total_items": len(cards), "remaining": len(pending),
            "skipped": ",".join(sorted(skip_ids)) if len(todo) < len(pending) else "",
            "section_title": dict(fbk.SECTIONS)[fbk.section_of(card["main"]["sel"])], **_auth_ctx(),
        })


@router.post("/runs/{run_id}/selections/{sel_id}/triage")
async def triage_submit(run_id: str, sel_id: str, decision: str = Form(...), option: str = Form(""), tags: list[str] = Form([]),
                        note: str = Form(""), rule: str = Form(""), quantity: float | None = Form(None), return_to: str = Form("run"),
                        skipped: str = Form(""), sub_allowed: str = Form(""), sub_note: str = Form("")):
    msg = err = None
    with session_scope() as session:
        try:
            if decision == "keep" and option and option != sel_id:
                decision = "choose"
            if decision in ("keep", "choose"):
                # Substitution preference for the item being kept (recorded as feedback if it differs from the agent's).
                await service.set_substitution(session, context.client(), selection_id=option if decision == "choose" else sel_id,
                                               allowed=bool(sub_allowed), note=sub_note, in_cart=False)
            # Only keep tags that fit the decision actually taken (the form shows all chip groups).
            action = {"keep": "approved", "remove": "removed", "choose": "chose_alternative"}.get(decision, "")
            tags = [t for t in tags if t in FEEDBACK_TAGS.get(action, {}) or t == "other"]
            fbk.triage(session, sel_id, decision=decision, chosen_selection_id=option or None, tags=tags, note=note,
                       rule=rule or None, quantity=quantity)
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}" + ("/triage" if return_to == "triage" else ""), err=str(exc))
        if decision in ("keep", "choose"):
            msg, err = await _add_to_cart(session, run_id, [option if decision == "choose" else sel_id])
    target = option or sel_id
    if return_to == "triage":
        return _back(f"/runs/{run_id}/triage" + (f"?skipped={quote(skipped)}" if skipped else ""), msg=msg, err=err)
    return _back(f"/runs/{run_id}", msg=msg, err=err, anchor=f"card-{target}")


@router.post("/runs/{run_id}/approve-all")
async def approve_all(run_id: str, snapshot: str = Form("")):
    with session_scope() as session:
        try:
            n = service.approve_all_current(session, run_id, [s for s in snapshot.split(",") if s])
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
        msg, err = await _add_to_cart(session, run_id, None)
    lead = f"Approved {n} item(s)." if n else ""
    return _back(f"/runs/{run_id}", msg=" ".join(x for x in [lead, msg] if x) or None, err=err)


@router.post("/runs/{run_id}/reject-remaining")
def reject_rest(run_id: str, snapshot: str = Form("")):
    with session_scope() as session:
        try:
            n = service.reject_remaining(session, run_id, [s for s in snapshot.split(",") if s])
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", msg=f"Removed {n} pending item(s).")


@router.post("/runs/{run_id}/apply")
async def apply(run_id: str):
    with session_scope() as session:
        msg, err = await _add_to_cart(session, run_id, None)
    return _back(f"/runs/{run_id}", msg=msg or ("Nothing waiting to be added." if not err else None), err=err)


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str):
    with session_scope() as session:
        try:
            service.cancel_run(session, run_id)
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", msg="Run cancelled.")


# -- modify (from the cart or a run) -----------------------------------------------------------

async def _modify_page(request: Request, *, product_id: str, sel_id: str | None, q: str | None, back: str):
    client = context.client()
    with session_scope() as session:
        sel = session.get(Selection, sel_id) if sel_id else None
        if sel is None and not sel_id:
            sel = mod.linked_selection(session, product_id)
        cart_line, cart_error = None, None
        if _connected():
            try:
                cart = await client.get_cart()
                cart_line = next((l for l in cart.lines if l.product_id == product_id), None)
            except (AuthRequired, RetailerError) as exc:
                cart_error = str(exc)
        current = None
        try:
            current = await client.get_product(product_id)
            service.record_observation(session, current, source="product", store_id=settings.store_id)
        except RetailerError as exc:
            cart_error = cart_error or str(exc)
        product = session.scalar(select(Product).where(Product.retailer_product_id == product_id))
        if product is None and cart_line is not None:
            product = service._product_from_cart_line(session, cart_line)  # noqa: SLF001
        if product is None:
            return HTMLResponse("Unknown product.", status_code=404)
        query = (q if q is not None else (sel.requirement if sel and sel.requirement else product.name)) or ""
        results, search_error = [], None
        if query.strip():
            try:
                found = (await client.search_products(query))["products"]
                found = [p for p in found if p.retailer_product_id != product_id]
                fam = product.comparison_family
                found.sort(key=lambda p: (p.comparison_family != fam, p.unit_price is None, p.unit_price or 0))
                found = found[:24]
                for p in found:
                    service.record_observation(session, p, source="search", store_id=settings.store_id)
                prefs = service.active_preferences(session)
                fb_hist = fbk.feedback_by_product(session, [p.retailer_product_id for p in found])
                results = [{"np": p, "flags": service.preference_flags(prefs, p), "fb": fb_hist.get(p.retailer_product_id)} for p in found]
            except RetailerError as exc:
                search_error = str(exc)
        options = []
        if sel and sel.choice_group:
            options = [s for s in sel.choice_group.selections if s.id != sel.id]
        state = ("In cart ×" + _qty(cart_line.quantity)) if cart_line else (STATUS_LABELS.get(sel.status) if sel else "Not in cart")
        return templates.TemplateResponse(request, "modify.html", {
            "product": product, "current": current, "sel": sel, "cart_line": cart_line, "state": state, "options": options,
            "results": results, "query": query, "search_error": search_error, "cart_error": cart_error, "back": back,
            "stats": service.price_stats(session, product, store_id=settings.store_id),
            "events": act.activity(session, days=365, product_id=product_id, limit=15), **_auth_ctx(),
        })


@router.get("/cart/{product_id}/modify", response_class=HTMLResponse)
async def modify_from_cart(request: Request, product_id: str, q: str | None = None):
    return await _modify_page(request, product_id=product_id, sel_id=None, q=q, back="/cart")


@router.get("/runs/{run_id}/selections/{sel_id}/modify", response_class=HTMLResponse)
async def modify_from_run(request: Request, run_id: str, sel_id: str, q: str | None = None):
    with session_scope() as session:
        sel = session.get(Selection, sel_id)
        if sel is None or sel.shopping_run_id != run_id:
            return HTMLResponse("Unknown item.", status_code=404)
        pid = sel.product.retailer_product_id
    return await _modify_page(request, product_id=pid, sel_id=sel_id, q=q, back=f"/runs/{run_id}#card-{sel_id}")


@router.post("/modify")
async def modify_submit(action: str = Form(...), product_id: str = Form(...), selection_id: str = Form(""), back: str = Form("/cart"),
                        new_product_id: str = Form(""), quantity: float | None = Form(None), tags: list[str] = Form([]),
                        note: str = Form(""), rule: str = Form(""), in_cart: str = Form(""),
                        sub_allowed: str = Form(""), sub_note: str = Form("")):
    if not back.startswith("/"):
        back = "/cart"
    with session_scope() as session:
        try:
            if action in ("substitution", "substitute_with"):
                if action == "substitute_with" and not new_product_id:
                    raise service.ShoppingError("Pick a product in the list to use as the substitute.")
                msg = await service.set_substitution(
                    session, context.client(), selection_id=selection_id or None, product_id=product_id,
                    allowed=bool(sub_allowed) or action == "substitute_with", note=sub_note,
                    substitute_product_id=new_product_id if action == "substitute_with" else None, in_cart=bool(in_cart))
                return _back(back, msg=msg)
            msg = await mod.modify(session, context.client(), action=action, product_id=product_id, selection_id=selection_id or None,
                                   new_product_id=new_product_id or None, quantity=quantity, tags=tags, note=note, rule=rule or None,
                                   in_cart=bool(in_cart))
        except AuthRequired as exc:
            return _back(back, err=f"Saved in the app, but the cart wasn't changed: {exc}")
        except (service.ShoppingError, RetailerError) as exc:
            return _back(back, err=str(exc))
    return _back(back, msg=msg)


# -- history: activity / prices / feedback / rules / product -------------------------------------

@router.get("/history", response_class=HTMLResponse)
def history_activity(request: Request, kind: str = "", q: str = "", days: int = 30, run_id: str = ""):
    with session_scope() as session:
        events = act.activity(session, days=days, kind=kind or None, q=q or None, run_id=run_id or None)
        run = session.get(ShoppingRun, run_id) if run_id else None
        return templates.TemplateResponse(request, "activity.html", {"events": events, "kind": kind, "q": q, "days": days, "run": run,
                                                                      "tab": "activity", **_auth_ctx()})


@router.get("/history/prices", response_class=HTMLResponse)
def history_prices(request: Request, q: str = ""):
    with session_scope() as session:
        query = (
            select(Product, func.count(PriceObservation.id), func.max(PriceObservation.observed_at))
            .join(PriceObservation, PriceObservation.product_id == Product.id)
            .group_by(Product.id)
            .order_by(func.max(PriceObservation.observed_at).desc())
            .limit(100)
        )
        if q:
            like = f"%{q.lower()}%"
            query = query.where(func.lower(Product.name).like(like) | func.lower(Product.brand).like(like))
        rows = session.execute(query).all()
        changes = service.recent_price_changes(session, days=90, limit=20, store_id=settings.store_id)
        return templates.TemplateResponse(request, "history.html", {"rows": rows, "q": q, "changes": changes, "tab": "prices", **_auth_ctx()})


@router.get("/history/feedback", response_class=HTMLResponse)
def history_feedback(request: Request, action: str = "", tag: str = "", requirement: str = "", unreviewed: str = "", ids: str = ""):
    with session_scope() as session:
        if ids:
            wanted = {int(i) for i in ids.split(",") if i.isdigit()}
            rows = [r for r in fbk.list_feedback(session, limit=1000) if r.id in wanted]
        else:
            rows = fbk.list_feedback(session, action=action or None, tag=tag or None, requirement=requirement or None,
                                     unreviewed_only=bool(unreviewed))
        f = {"action": action, "tag": tag, "requirement": requirement, "unreviewed": bool(unreviewed),
             "any": bool(action or tag or requirement or unreviewed or ids)}
        return templates.TemplateResponse(request, "feedback.html", {
            "rows": [fbk.feedback_dict(r) for r in rows], "summary": fbk.feedback_summary(session), "f": f, "tab": "feedback", **_auth_ctx(),
        })


@router.get("/history/rules", response_class=HTMLResponse)
def history_rules(request: Request, show_inactive: bool = False):
    with session_scope() as session:
        q = select(Preference).order_by(Preference.active.desc(), Preference.created_at.desc())
        if not show_inactive:
            q = q.where(Preference.active.is_(True))
        return templates.TemplateResponse(request, "preferences.html", {"prefs": list(session.scalars(q)), "show_inactive": show_inactive,
                                                                         "tab": "rules", **_auth_ctx()})


@router.get("/feedback")
def feedback_redirect():
    return RedirectResponse("/history/feedback", status_code=301)


@router.get("/preferences")
def preferences_redirect():
    return RedirectResponse("/history/rules", status_code=301)


@router.get("/history/{product_id}", response_class=HTMLResponse)
def product_page(request: Request, product_id: str):
    with session_scope() as session:
        try:
            data = service.product_history(session, product_id, limit=200)
        except service.ShoppingError as exc:
            return HTMLResponse(str(exc), status_code=404)
        product = session.scalar(select(Product).where(Product.retailer_product_id == product_id))
        events = act.activity(session, days=3650, product_id=product_id, limit=200)
        return templates.TemplateResponse(request, "product.html", {"d": data, "product": product, "events": events, "tab": "prices", **_auth_ctx()})


@router.post("/feedback/review")
def feedback_review(ids: list[int] = Form([]), resolution: str = Form("")):
    with session_scope() as session:
        n = fbk.mark_reviewed(session, ids, resolution)
    return _back("/history/feedback", msg=f"Marked {n} record(s) reviewed.")


@router.post("/feedback/pattern")
def feedback_pattern(kind: str = Form(...), scope: str = Form(""), target: str = Form(""), target_label: str = Form(""), ids: str = Form("")):
    with session_scope() as session:
        try:
            pref = service.add_preference(session, kind=kind, scope=scope or None, target=target or None, target_label=target_label or None)
        except service.ShoppingError as exc:
            return _back("/history/feedback", err=str(exc))
        fbk.mark_reviewed(session, [int(i) for i in ids.split(",") if i.isdigit()], f"Rule #{pref.id} created")
    return _back("/history/feedback", msg=f"Rule saved: {PREFERENCE_KINDS.get(kind, kind)}" + (f" — {target_label}" if target_label else ""))


@router.post("/feedback/note")
def feedback_note(note: str = Form(...), requirement: str = Form("")):
    with session_scope() as session:
        try:
            fbk.add_note(session, note=note, requirement=requirement.strip() or None)
        except service.ShoppingError as exc:
            return _back("/history/feedback", err=str(exc))
    return _back("/history/feedback", msg="Note saved.")


@router.post("/preferences")
def add_pref(kind: str = Form(...), scope: str = Form(""), target: str = Form(""), note: str = Form("")):
    with session_scope() as session:
        try:
            service.add_preference(session, kind=kind, scope=scope.strip() or None, target=target.strip() or None,
                                   target_label=target.strip() or None, note=note.strip() or None)
        except service.ShoppingError as exc:
            return _back("/history/rules", err=str(exc))
    return _back("/history/rules", msg="Rule saved.")


@router.post("/preferences/{pref_id}/toggle")
def toggle_pref(pref_id: int):
    with session_scope() as session:
        pref = session.get(Preference, pref_id)
        if pref:
            pref.active = not pref.active
    return _back("/history/rules", msg="Rule updated.")


@router.post("/preferences/{pref_id}/edit")
def edit_pref(pref_id: int, scope: str = Form(""), note: str = Form(""), weight: float = Form(1.0)):
    with session_scope() as session:
        pref = session.get(Preference, pref_id)
        if pref:
            pref.scope, pref.note, pref.weight = scope.strip() or None, note.strip() or None, weight
    return _back("/history/rules", msg="Rule updated.")


# -- past orders -----------------------------------------------------------------------------

async def _order_summaries() -> tuple[list, str | None]:
    if not _connected():
        return [], "Connect your No Frills session (Auth) to load your order history."
    try:
        return await context.client().list_past_orders(), None
    except (AuthRequired, RetailerError) as exc:
        return [], str(exc)


async def _import(session, order_id: str, summaries) -> PastOrder:
    data = await context.client().get_past_order(order_id)
    summary = next((s for s in summaries if s.order_id == order_id), None)
    return ords.import_order(session, data, summary, store_id=settings.store_id)


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    summaries, error = await _order_summaries()
    with session_scope() as session:
        imported = {o.id: o for o in session.scalars(select(PastOrder).order_by(PastOrder.placed_at.desc()))}
        rows = [ords.summary_dict(s, imported.get(s.order_id)) for s in summaries]
        if not rows:  # offline: show what was imported earlier
            rows = [{"order_id": o.id, "placed_at": o.placed_at.isoformat() if o.placed_at else None, "total": float(o.total) if o.total is not None else None,
                     "store": o.store, "fulfillment": o.fulfillment, "imported": True} for o in imported.values()]
        return templates.TemplateResponse(request, "orders.html", {"rows": rows, "error": error, **_auth_ctx()})


@router.post("/orders/import")
async def orders_import():
    summaries, error = await _order_summaries()
    if error:
        return _back("/orders", err=error)
    n = 0
    with session_scope() as session:
        for s in summaries:
            try:
                await _import(session, s.order_id, summaries)
                n += 1
            except (AuthRequired, RetailerError) as exc:
                return _back("/orders", msg=f"Imported {n} order(s).", err=str(exc))
    return _back("/orders", msg=f"Imported {n} order(s) — purchases and prices paid are now in History.")


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_page(request: Request, order_id: str):
    error = None
    with session_scope() as session:
        if _connected():
            summaries, error = await _order_summaries()
            try:
                order = await _import(session, order_id, summaries)
            except (AuthRequired, RetailerError) as exc:
                error, order = str(exc), session.get(PastOrder, order_id)
        else:
            order = session.get(PastOrder, order_id)
            error = None if order else "Connect your No Frills session (Auth) to load this order."
        if order is None:
            return templates.TemplateResponse(request, "order.html", {"o": None, "error": error or "Order not found.", **_auth_ctx()})
        return templates.TemplateResponse(request, "order.html", {"o": ords.order_view(session, order), "error": error, **_auth_ctx()})


# -- cart ------------------------------------------------------------------------------------

@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request):
    cart, error, origins = None, None, {}
    try:
        cart = await context.client().get_cart()
    except (AuthRequired, RetailerError) as exc:
        error = str(exc)
    if cart:
        with session_scope() as session:
            for line in cart.lines:
                sel = mod.linked_selection(session, line.product_id)
                if sel:
                    origins[line.product_id] = {"run_id": sel.shopping_run_id, "run": sel.run.summary, "requirement": sel.requirement}
    return templates.TemplateResponse(request, "cart.html", {"cart": cart, "error": error, "origins": origins, **_auth_ctx()})


@router.post("/cart/{product_id}")
async def cart_update(product_id: str, quantity: float = Form(...)):
    with session_scope() as session:
        try:
            await service.user_set_cart_quantity(session, context.client(), product_id, max(quantity, 0), source="cart_page",
                                                 selection=mod.linked_selection(session, product_id))
        except (AuthRequired, RetailerError) as exc:
            return _back("/cart", err=str(exc))
    return _back("/cart", msg="Cart updated." if quantity > 0 else "Removed from cart.")


@router.post("/cart/{product_id}/substitution")
async def cart_substitution(product_id: str, allowed: str = Form("")):
    with session_scope() as session:
        sel = mod.linked_selection(session, product_id)
        try:
            msg = await service.set_substitution(session, context.client(), selection_id=sel.id if sel else None, product_id=product_id,
                                                 allowed=bool(allowed), note=sel.substitution_note if sel else None, in_cart=True)
        except (AuthRequired, RetailerError, service.ShoppingError) as exc:
            return _back("/cart", err=str(exc))
    return _back("/cart", msg=msg)


# -- auth ------------------------------------------------------------------------------------

@router.get("/auth", response_class=HTMLResponse)
def auth_page(request: Request):
    return templates.TemplateResponse(request, "auth.html", _auth_ctx())


@router.post("/auth")
def auth_set(token: str = Form(...)):
    try:
        context.auth.set_access_token(token)
    except ValueError as exc:
        return _back("/auth", err=str(exc))
    context.client()._cart_id = None  # noqa: SLF001 - rediscover the cart for the new session
    return _back("/auth", msg="Session saved.")


@router.post("/auth/clear")
async def auth_clear():
    await context.auth.clear()
    return _back("/auth", msg="Session cleared.")
