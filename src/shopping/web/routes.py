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
from shopping.core import service
from shopping.database import session_scope
from shopping.models import PREFERENCE_KINDS, REASON_CODES, PriceObservation, Product, Selection, ShoppingRun
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
templates.env.globals.update(unit_price=_unit, REASON_CODES=REASON_CODES, PREFERENCE_KINDS=PREFERENCE_KINDS, store_id=settings.store_id)


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


def _card(session, sel: Selection, prefs) -> dict:
    obs = sel.observation
    stats = service.price_stats(session, sel.product, store_id=obs.store_id if obs else None)
    return {"sel": sel, "p": sel.product, "obs": obs, "stats": stats, "flags": service.preference_flags(prefs, sel.product)}


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    with session_scope() as session:
        try:
            run = service.get_run(session, run_id)
        except service.ShoppingError as exc:
            return HTMLResponse(str(exc), status_code=404)
        prefs = service.active_preferences(session)
        # One card per requirement: standalone selection, or a choice group with its options.
        cards = []
        for sel in run.selections:
            if sel.choice_group_id is None:
                cards.append({"kind": "single", "pos": sel.position, "created": sel.created_at, "main": _card(session, sel, prefs), "options": []})
        for g in run.choice_groups:
            chosen = next((s for s in g.selections if s.id == g.selected_selection_id), g.selections[0])
            cards.append({"kind": "group", "group": g, "pos": g.position, "created": g.created_at, "main": _card(session, chosen, prefs),
                          "options": [_card(session, s, prefs) for s in g.selections]})
        cards.sort(key=lambda c: (c["created"], c["pos"]))
        snapshot = [c["main"]["sel"].id for c in cards]
        counts = {}
        for c in cards:
            counts[c["main"]["sel"].status] = counts.get(c["main"]["sel"].status, 0) + 1
        return templates.TemplateResponse(request, "run.html", {
            "run": run, "cards": cards, "snapshot": ",".join(snapshot), "counts": counts,
            "total": service.estimated_total(run), "editable": run.status not in ("applied", "cancelled"), **_auth_ctx(),
        })


@router.post("/runs/{run_id}/selections/{sel_id}/decide")
def decide(run_id: str, sel_id: str, decision: str = Form(...)):
    with session_scope() as session:
        try:
            service.decide_selection(session, sel_id, approve=(decision == "approve"))
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", anchor=f"card-{sel_id}")


@router.post("/runs/{run_id}/selections/{sel_id}/quantity")
def set_quantity(run_id: str, sel_id: str, quantity: float = Form(...)):
    with session_scope() as session:
        try:
            service.user_set_selection_quantity(session, sel_id, quantity)
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", anchor=f"card-{sel_id}")


@router.post("/runs/{run_id}/groups/{group_id}/choose")
def choose(run_id: str, group_id: str, selection_id: str = Form(...)):
    with session_scope() as session:
        try:
            service.choose_alternative(session, group_id, selection_id)
        except service.ShoppingError as exc:
            return _back(f"/runs/{run_id}", err=str(exc))
    return _back(f"/runs/{run_id}", anchor=f"group-{group_id}")


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


@router.post("/runs/{run_id}/selections/{sel_id}/preference")
def quick_preference(run_id: str, sel_id: str, kind: str = Form(...)):
    with session_scope() as session:
        sel = session.get(Selection, sel_id)
        if sel is None:
            return _back(f"/runs/{run_id}", err="Unknown selection.")
        p = sel.product
        scope = sel.requirement
        if kind in ("prefer_product", "blacklist_product", "prefer_for_type"):
            target, label = p.retailer_product_id, f"{p.brand + ' ' if p.brand else ''}{p.name} {p.raw_size or ''}".strip()
        elif kind in ("prefer_brand", "blacklist_brand"):
            if not p.brand:
                return _back(f"/runs/{run_id}", err="This product has no brand recorded.")
            target, label = p.brand, p.brand
            scope = None if kind == "blacklist_brand" else scope
        else:
            return _back(f"/runs/{run_id}", err="Unsupported preference.")
        if kind == "blacklist_product":
            scope = None
        service.add_preference(session, kind=kind, scope=scope, target=target, target_label=label, source_selection_id=sel.id)
    return _back(f"/runs/{run_id}", msg=f"Saved: {PREFERENCE_KINDS[kind]} — {label}" + (f" (for {scope})" if scope else ""), anchor=f"card-{sel_id}")


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
