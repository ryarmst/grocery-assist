"""Server-rendered web UI: run review, history, preferences, cart and auth status."""

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
from shopping.core import feedback as fbk
from shopping.core import service
from shopping.database import session_scope
from shopping.models import (
    FEEDBACK_TAG_LABELS,
    FEEDBACK_TAGS,
    PREFERENCE_KINDS,
    Feedback,
    REASON_CODES,
    PriceObservation,
    Product,
    ShoppingRun,
)
from shopping.retailer.client import RetailerError

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


templates.env.filters.update(money=_money, local=_local, qty=_qty)
templates.env.globals.update(unit_price=_unit, REASON_CODES=REASON_CODES, PREFERENCE_KINDS=PREFERENCE_KINDS, FEEDBACK_TAGS=FEEDBACK_TAGS,
                             FEEDBACK_TAG_LABELS=FEEDBACK_TAG_LABELS, store_id=settings.store_id)


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


# -- pages --------------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    with session_scope() as session:
        runs = list(session.scalars(select(ShoppingRun).order_by(ShoppingRun.created_at.desc()).limit(30)))
        rows = [{"run": r, "items": len(service.chosen_selections(r)), "total": service.estimated_total(r),
                 "pending": sum(1 for s in service.chosen_selections(r) if s.status == "pending")} for r in runs]
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
        # Price history and feedback from earlier runs only (not the decision just made in this run).
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


def _status_counts(cards) -> dict:
    counts: dict[str, int] = {}
    for c in cards:
        counts[c["main"]["sel"].status] = counts.get(c["main"]["sel"].status, 0) + 1
    return counts


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    with session_scope() as session:
        try:
            run = service.get_run(session, run_id)
        except service.ShoppingError as exc:
            return HTMLResponse(str(exc), status_code=404)
        cards = _cards(session, run)
        sections = [(key, title, [c for c in cards if fbk.section_of(c["main"]["sel"]) == key]) for key, title in fbk.SECTIONS]
        return templates.TemplateResponse(request, "run.html", {
            "run": run, "sections": sections, "cards": cards, "snapshot": ",".join(c["main"]["sel"].id for c in cards),
            "counts": _status_counts(cards), "total": service.estimated_total(run),
            "editable": run.status not in ("applied", "cancelled"), **_auth_ctx(),
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
        if not todo or run.status in ("applied", "cancelled"):
            return _back(f"/runs/{run_id}", msg="Nothing left to triage. Review the list, then apply approved items to the cart.")
        card = todo[0]
        return templates.TemplateResponse(request, "triage.html", {
            "run": run, "card": card, "done": len(cards) - len(pending), "total_items": len(cards), "remaining": len(pending),
            "skipped": ",".join(sorted(skip_ids)) if len(todo) < len(pending) else "",
            "section_title": dict(fbk.SECTIONS)[fbk.section_of(card["main"]["sel"])], **_auth_ctx(),
        })


@router.post("/runs/{run_id}/selections/{sel_id}/triage")
def triage_submit(run_id: str, sel_id: str, decision: str = Form(...), option: str = Form(""), tags: list[str] = Form([]),
                  note: str = Form(""), rule: str = Form(""), quantity: float | None = Form(None), return_to: str = Form("run"),
                  skipped: str = Form("")):
    with session_scope() as session:
        try:
            if decision == "keep" and option and option != sel_id:
                decision = "choose"
            # Only keep tags that fit the decision actually taken (the form shows all chip groups).
            action = {"keep": "approved", "remove": "removed", "choose": "chose_alternative"}.get(decision, "")
            tags = [t for t in tags if t in FEEDBACK_TAGS.get(action, {}) or t == "other"]
            fbk.triage(session, sel_id, decision=decision, chosen_selection_id=option or None, tags=tags, note=note,
                       rule=rule or None, quantity=quantity)
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}" + ("/triage" if return_to == "triage" else ""), err=str(exc))
    if return_to == "triage":
        return _back(f"/runs/{run_id}/triage" + (f"?skipped={quote(skipped)}" if skipped else ""))
    return _back(f"/runs/{run_id}", anchor=f"card-{option or sel_id}")


@router.post("/runs/{run_id}/approve-all")
def approve_all(run_id: str, snapshot: str = Form("")):
    with session_scope() as session:
        try:
            n = service.approve_all_current(session, run_id, [s for s in snapshot.split(",") if s])
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", msg=f"Approved {n} item(s). Items added after you loaded the page stay pending.")


@router.post("/runs/{run_id}/reject-remaining")
def reject_rest(run_id: str, snapshot: str = Form("")):
    with session_scope() as session:
        try:
            n = service.reject_remaining(session, run_id, [s for s in snapshot.split(",") if s])
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", msg=f"Rejected {n} pending item(s).")


@router.post("/runs/{run_id}/apply")
async def apply(run_id: str):
    with session_scope() as session:
        try:
            result = await service.apply_approved(session, context.client(), run_id)
        except (AuthRequired, RetailerError) as exc:
            return _back("/auth", err=f"Couldn't update the cart: {exc}")
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    if result.get("message"):
        return _back(f"/runs/{run_id}", msg=result["message"])
    msg = f"Added {result['applied']} item(s) to the cart."
    err = f"{result['failed']} item(s) failed — see the item cards." if result["failed"] else None
    return _back(f"/runs/{run_id}", msg=msg, err=err)


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str):
    with session_scope() as session:
        try:
            service.cancel_run(session, run_id)
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", msg="Run cancelled.")


# -- history ------------------------------------------------------------------------------

@router.get("/history", response_class=HTMLResponse)
def history(request: Request, q: str = ""):
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
        return templates.TemplateResponse(request, "history.html", {"rows": rows, "q": q, "changes": changes, **_auth_ctx()})


@router.get("/history/{product_id}", response_class=HTMLResponse)
def product_page(request: Request, product_id: str):
    with session_scope() as session:
        try:
            data = service.product_history(session, product_id, limit=200)
        except service.ShoppingError as exc:
            return HTMLResponse(str(exc), status_code=404)
        product = session.scalar(select(Product).where(Product.retailer_product_id == product_id))
        return templates.TemplateResponse(request, "product.html", {"d": data, "product": product, **_auth_ctx()})


# -- preferences ----------------------------------------------------------------------------

@router.get("/preferences", response_class=HTMLResponse)
def preferences(request: Request, show_inactive: bool = False):
    with session_scope() as session:
        from shopping.models import Preference

        q = select(Preference).order_by(Preference.active.desc(), Preference.created_at.desc())
        if not show_inactive:
            q = q.where(Preference.active.is_(True))
        prefs = list(session.scalars(q))
        return templates.TemplateResponse(request, "preferences.html", {"prefs": prefs, "show_inactive": show_inactive, **_auth_ctx()})


@router.post("/preferences")
def add_pref(kind: str = Form(...), scope: str = Form(""), target: str = Form(""), note: str = Form("")):
    with session_scope() as session:
        try:
            service.add_preference(session, kind=kind, scope=scope.strip() or None, target=target.strip() or None,
                                   target_label=target.strip() or None, note=note.strip() or None)
        except service.ShoppingError as exc:
            return _back("/preferences", err=str(exc))
    return _back("/preferences", msg="Preference saved.")


@router.post("/preferences/{pref_id}/toggle")
def toggle_pref(pref_id: int):
    with session_scope() as session:
        from shopping.models import Preference

        pref = session.get(Preference, pref_id)
        if pref:
            pref.active = not pref.active
    return _back("/preferences", msg="Preference updated.")


@router.post("/preferences/{pref_id}/edit")
def edit_pref(pref_id: int, scope: str = Form(""), note: str = Form(""), weight: float = Form(1.0)):
    with session_scope() as session:
        from shopping.models import Preference

        pref = session.get(Preference, pref_id)
        if pref:
            pref.scope, pref.note, pref.weight = scope.strip() or None, note.strip() or None, weight
    return _back("/preferences", msg="Preference updated.")


# -- feedback ---------------------------------------------------------------------------------

@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, action: str = "", tag: str = "", requirement: str = "", unreviewed: str = "", ids: str = ""):
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
            "rows": [fbk.feedback_dict(r) for r in rows], "summary": fbk.feedback_summary(session), "f": f, **_auth_ctx(),
        })


@router.post("/feedback/review")
def feedback_review(ids: list[int] = Form([]), resolution: str = Form("")):
    with session_scope() as session:
        n = fbk.mark_reviewed(session, ids, resolution)
    return _back("/feedback", msg=f"Marked {n} record(s) reviewed.")


@router.post("/feedback/pattern")
def feedback_pattern(kind: str = Form(...), scope: str = Form(""), target: str = Form(""), target_label: str = Form(""), ids: str = Form("")):
    with session_scope() as session:
        try:
            pref = service.add_preference(session, kind=kind, scope=scope or None, target=target or None, target_label=target_label or None)
        except service.ShoppingError as exc:
            return _back("/feedback", err=str(exc))
        fbk.mark_reviewed(session, [int(i) for i in ids.split(",") if i.isdigit()], f"Rule #{pref.id} created")
    return _back("/feedback", msg=f"Rule saved: {PREFERENCE_KINDS.get(kind, kind)}" + (f" — {target_label}" if target_label else ""))


@router.post("/feedback/note")
def feedback_note(note: str = Form(...), requirement: str = Form("")):
    with session_scope() as session:
        try:
            fbk.add_note(session, note=note, requirement=requirement.strip() or None)
        except service.ShoppingError as exc:
            return _back("/feedback", err=str(exc))
    return _back("/feedback", msg="Note saved.")


# -- cart ------------------------------------------------------------------------------------

@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request):
    cart, error = None, None
    try:
        cart = await context.client().get_cart()
    except (AuthRequired, RetailerError) as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "cart.html", {"cart": cart, "error": error, **_auth_ctx()})


@router.post("/cart/{product_id}")
async def cart_update(product_id: str, quantity: float = Form(...)):
    with session_scope() as session:
        try:
            await service.user_set_cart_quantity(session, context.client(), product_id, max(quantity, 0))
        except (AuthRequired, RetailerError) as exc:
            return _back("/cart", err=str(exc))
    return _back("/cart", msg="Cart updated." if quantity > 0 else "Removed from cart.")


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
