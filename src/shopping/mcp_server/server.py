"""MCP interface for the shopping agent.

A deliberately small, semantic tool surface. There are no tools for checkout, orders,
payment, timeslots or arbitrary HTTP. The only cart-changing tool applies items the
*user* has approved in the web UI.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field
from sqlalchemy import select

from shopping import context
from shopping.config import settings
from shopping.core import feedback as fbk
from shopping.core import service
from shopping.database import session_scope
from shopping.models import REASON_CODES, ShoppingRun

log = logging.getLogger("shopping.mcp")

INSTRUCTIONS = f"""\
Grocery shopping assistant for No Frills (store {settings.store_id}). Workflow:
1. shopping_get_preferences, then search products / deals (prices include our own history where available).
2. shopping_create_run, then stage items with shopping_add_selection (one product) or
   shopping_add_choice_group (several candidate products for one requirement, with a recommendation).
   Always give reason codes and a short rationale. Reason codes: {", ".join(REASON_CODES)}.
   Use the exact item name from the user's list/pantry as `requirement` so history lines up across runs.
   For regular purchases, check shopping_get_requirement_history first and only stage items that are due.
3. shopping_submit_run_for_review and give the user the review URL. The user triages items in the web UI,
   often recording why they removed or swapped something; read shopping_get_feedback to learn from it.
4. Approving an item in the web UI adds it to the cart immediately. shopping_apply_approved only retries
   approved items that couldn't be added (e.g. the session had expired); never use it on unapproved items.
This service cannot check out or place orders; the user completes purchases themselves.
Blacklist preferences are absolute; other preferences are guidance.
"""

mcp = MCPServer(name="shopping-assistant", instructions=INSTRUCTIONS, version="0.1.0")


def logged(fn):
    """Log tool name, duration, outcome and key identifiers (never secrets)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        started = time.monotonic()
        ids = {k: v for k, v in kwargs.items() if k in ("run_id", "product_id", "selection_id", "query") and v}
        try:
            result = await fn(*args, **kwargs)
            log.info("mcp tool=%s ok duration_ms=%.0f %s", fn.__name__, (time.monotonic() - started) * 1000, ids)
            return result
        except Exception as exc:
            log.warning("mcp tool=%s failed duration_ms=%.0f %s error=%s", fn.__name__, (time.monotonic() - started) * 1000, ids, exc)
            raise

    return wrapper


def _review_url(run_id: str) -> str:
    return f"{context.PUBLIC_URL}/runs/{run_id}"


def _annotate(session, products, source: str) -> list[dict]:
    """Record observations, then add preference flags and our own price history."""
    prefs = service.active_preferences(session)
    for p in products:
        service.record_observation(session, p, source=source, store_id=settings.store_id)
    ids = [p.retailer_product_id for p in products]
    history = service.history_brief(session, ids, settings.store_id)
    past_feedback = fbk.feedback_by_product(session, ids)
    out = []
    for p in products:
        d = p.summary()
        flags = service.preference_flags(prefs, p)
        if flags:
            d["preferences"] = flags
        if p.retailer_product_id in history:
            d["history"] = history[p.retailer_product_id]
        if p.retailer_product_id in past_feedback:
            d["user_feedback"] = past_feedback[p.retailer_product_id]
        out.append({k: v for k, v in d.items() if v is not None})
    return out


# -------------------------------------------------------------------------------------
# Catalogue
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_search_products(
    query: Annotated[str, Field(description="Search text, e.g. 'peanut butter'")],
    page: Annotated[int, Field(ge=1, le=20, description="Result page (48 per page)")] = 1,
    limit: Annotated[int, Field(ge=1, le=48, description="Max products to return")] = 24,
    brand: Annotated[str | None, Field(description="Only products whose brand contains this text")] = None,
    comparison_family: Annotated[str | None, Field(description="weight | volume | count")] = None,
    sort_by_unit_price: Annotated[bool, Field(description="Sort cheapest per 100 g / 100 ml / item first")] = False,
) -> dict:
    """Search current No Frills products. Returns normalized prices, unit prices, deals,
    preference flags and our own price history where we have it. Sponsored results are excluded."""
    res = await context.client().search_products(query, page=page)
    products = res["products"]
    if brand:
        products = [p for p in products if brand.lower() in (p.brand or "").lower()]
    if comparison_family:
        products = [p for p in products if p.comparison_family == comparison_family]
    if sort_by_unit_price:
        products.sort(key=lambda p: (p.comparison_family, p.unit_price is None, p.unit_price or 0))
    with session_scope() as session:
        results = _annotate(session, products[:limit], "search")
    return {"query": query, "page": page, "total_results": res["total_results"], "returned": len(results), "results": results}


@mcp.tool()
@logged
async def shopping_get_product(product_id: Annotated[str, Field(description="Retailer product id, e.g. 20039581001_EA")]) -> dict:
    """Current details for one product plus our observed price history statistics."""
    p = await context.client().get_product(product_id)
    with session_scope() as session:
        service.record_observation(session, p, source="product", store_id=settings.store_id)
        product = service.upsert_product(session, p)
        d = p.summary()
        d["gtin"] = p.gtin
        d["history"] = service.price_stats(session, product, store_id=settings.store_id)
        flags = service.preference_flags(service.active_preferences(session), p)
        if flags:
            d["preferences"] = flags
    return {k: v for k, v in d.items() if v is not None}


@mcp.tool()
@logged
async def shopping_get_deals(
    kind: Annotated[str, Field(description="'flyer' (weekly flyer items) or 'price_reduction' (all items with a reduced price)")] = "flyer",
    query: Annotated[str, Field(description="Optional search text to narrow deals")] = "",
    category_code: Annotated[str | None, Field(description="Optional aisle category code from search filter groups")] = None,
    min_discount_pct: Annotated[float | None, Field(description="Only items at least this % below regular price")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
) -> dict:
    """Current deals. Includes retailer deal text (SALE / LIMIT / PC Optimum points) and, where we have
    history, how the price compares with what we've observed before."""
    products = await context.client().get_deals(query=query, kind=kind, category_code=category_code, min_discount_pct=min_discount_pct, limit=limit)
    with session_scope() as session:
        results = _annotate(session, products, "deals")
    return {"kind": kind, "query": query, "returned": len(results), "results": results}


# -------------------------------------------------------------------------------------
# Cart (read) and apply approved
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_cart() -> dict:
    """The current retailer cart (requires the user to have connected their session on /auth)."""
    cart = await context.client().get_cart()
    return cart.summary()


@mcp.tool()
@logged
async def shopping_apply_approved(run_id: str) -> dict:
    """Add the items the USER approved in this run to the retailer cart. Pending or rejected items are
    never added. Quantities are raised to the approved amount, never lowered. Does not check out."""
    with session_scope() as session:
        result = await service.apply_approved(session, context.client(), run_id)
        result["run_status"] = service.get_run(session, run_id).status
    return result


@mcp.tool()
@logged
async def shopping_auth_status() -> dict:
    """Whether the retailer session is connected (needed for cart operations). Contains no secrets."""
    st = context.auth.status()
    return {"state": st.state, "label": st.label, "seconds_remaining": st.seconds_remaining,
            "reconnect_url": f"{context.PUBLIC_URL}/auth"}


# -------------------------------------------------------------------------------------
# Runs and selections
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_create_run(summary: Annotated[str, Field(description="e.g. 'Weekly groceries'")], notes: str | None = None) -> dict:
    """Start a new shopping run to stage selections in."""
    with session_scope() as session:
        run = service.create_run(session, summary=summary, store_id=settings.store_id, notes=notes)
        return {"run_id": run.id, "status": run.status, "review_url": _review_url(run.id)}


@mcp.tool()
@logged
async def shopping_get_run(run_id: str, include_history: bool = False) -> dict:
    """A run with its selections, choice groups, statuses and estimated total."""
    with session_scope() as session:
        d = service.run_dict(session, service.get_run(session, run_id), with_history=include_history)
        d["review_url"] = _review_url(run_id)
        return d


@mcp.tool()
@logged
async def shopping_list_runs(limit: Annotated[int, Field(ge=1, le=50)] = 10, active_only: bool = False) -> dict:
    """Recent shopping runs, newest first."""
    with session_scope() as session:
        q = select(ShoppingRun).order_by(ShoppingRun.created_at.desc()).limit(limit)
        if active_only:
            q = q.where(ShoppingRun.status.in_(service.ACTIVE_RUN_STATES))
        runs = list(session.scalars(q))
        return {"runs": [{"run_id": r.id, "summary": r.summary, "status": r.status, "created_at": r.created_at.isoformat(),
                          "items": len(service.chosen_selections(r)), "estimated_total": float(round(service.estimated_total(r), 2))} for r in runs]}


@mcp.tool()
@logged
async def shopping_add_selection(
    run_id: str,
    product_id: Annotated[str, Field(description="Retailer product id from search results")],
    quantity: Annotated[float, Field(gt=0)] = 1,
    reasons: Annotated[list[str], Field(description=f"One or more of: {', '.join(REASON_CODES)}")] = [],  # noqa: B006
    rationale: Annotated[str | None, Field(description="One or two sentences explaining why")] = None,
    requirement: Annotated[str | None, Field(description="The need this fills, e.g. 'milk'")] = None,
) -> dict:
    """Stage one product in a run for the user to review. Fetches the current price and records it."""
    with session_scope() as session:
        sel = await service.add_selection(session, context.client(), run_id, product_id=product_id, quantity=quantity,
                                          reasons=reasons, rationale=rationale, requirement=requirement)
        return service.selection_dict(session, sel)


class ChoiceOption(BaseModel):
    product_id: str
    quantity: float = Field(1, gt=0)
    reasons: list[str] = []
    rationale: str | None = None


@mcp.tool()
@logged
async def shopping_add_choice_group(
    run_id: str,
    label: Annotated[str, Field(description="The requirement, e.g. 'Peanut butter'")],
    options: Annotated[list[ChoiceOption], Field(min_length=1, max_length=8)],
    recommended_index: Annotated[int, Field(ge=0, description="Index in options of your recommendation")] = 0,
) -> dict:
    """Stage several candidate products for one requirement. The user picks one; yours is preselected.
    Non-recommended options automatically get the 'alternative' reason."""
    with session_scope() as session:
        group = await service.add_choice_group(session, context.client(), run_id, label=label,
                                               options=[o.model_dump() for o in options], recommended_index=recommended_index)
        return {"choice_group_id": group.id, "label": group.label, "recommended_selection_id": group.recommended_selection_id,
                "options": [service.selection_dict(session, s) for s in group.selections]}


@mcp.tool()
@logged
async def shopping_update_selection(
    selection_id: str,
    quantity: Annotated[float | None, Field(gt=0)] = None,
    rationale: str | None = None,
    reasons: list[str] | None = None,
    withdraw: Annotated[bool, Field(description="Withdraw this suggestion")] = False,
) -> dict:
    """Change a pending selection's quantity, rationale or reasons, or withdraw it. Cannot approve."""
    with session_scope() as session:
        sel = service.update_selection(session, selection_id, quantity=quantity, rationale=rationale, reasons=reasons, withdraw=withdraw)
        return service.selection_dict(session, sel)


@mcp.tool()
@logged
async def shopping_submit_run_for_review(run_id: str) -> dict:
    """Mark the run ready for the user's review. Returns the URL to give the user."""
    with session_scope() as session:
        run = service.submit_run_for_review(session, run_id)
        return {"run_id": run.id, "status": run.status, "estimated_total": float(round(service.estimated_total(run), 2)),
                "review_url": _review_url(run.id)}


# -------------------------------------------------------------------------------------
# Preferences and history
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_preferences() -> dict:
    """Active shopping preferences. Blacklists (absolute=true) must be respected; others are guidance."""
    with session_scope() as session:
        return {"preferences": [service.preference_dict(p) for p in service.active_preferences(session)]}


@mcp.tool()
@logged
async def shopping_add_preference_note(
    note: Annotated[str, Field(description="The user's stated preference, in their words")],
    scope: Annotated[str | None, Field(description="Item type it applies to, e.g. 'coffee'")] = None,
) -> dict:
    """Record a free-form preference the user stated in conversation (e.g. 'whole bean coffee unless a big sale')."""
    with session_scope() as session:
        pref = service.add_preference(session, kind="preference_note", scope=scope, note=note)
        return service.preference_dict(pref)


@mcp.tool()
@logged
async def shopping_get_product_history(product_id: str, limit: Annotated[int, Field(ge=1, le=200)] = 30) -> dict:
    """Observed prices, package sizes and unit prices for a product over time, plus past selections."""
    with session_scope() as session:
        return service.product_history(session, product_id, limit=limit)


@mcp.tool()
@logged
async def shopping_get_selection_history(
    product_id: str | None = None,
    query: Annotated[str | None, Field(description="Match product name, brand or requirement, e.g. 'milk'")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
) -> dict:
    """Past suggestions: when, how often, typical quantity, approved vs rejected, price each time."""
    with session_scope() as session:
        return service.selection_history(session, product_id=product_id, query=query, limit=limit)


@mcp.tool()
@logged
async def shopping_get_recent_price_history(days: Annotated[int, Field(ge=1, le=365)] = 30, limit: Annotated[int, Field(ge=1, le=200)] = 30) -> dict:
    """Products whose price or package size changed recently (largest changes first)."""
    with session_scope() as session:
        return {"days": days, "changes": service.recent_price_changes(session, days=days, limit=limit, store_id=settings.store_id)}


# -------------------------------------------------------------------------------------
# Feedback and regular purchases
# -------------------------------------------------------------------------------------

@mcp.tool()
@logged
async def shopping_get_feedback(
    days: Annotated[int, Field(ge=1, le=730)] = 90,
    requirement: Annotated[str | None, Field(description="Only feedback about this item, e.g. 'milk'")] = None,
    product_id: str | None = None,
    action: Annotated[str | None, Field(description="removed | chose_alternative | approved | quantity_changed | note")] = None,
    unreviewed_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """What the user removed or swapped during review and why (reason tags + their own words), plus
    summary counts and detected patterns. Use it to avoid repeating suggestions the user rejected."""
    with session_scope() as session:
        rows = fbk.list_feedback(session, days=days, action=action, requirement=requirement,
                                 product_retailer_id=product_id, unreviewed_only=unreviewed_only, limit=limit)
        summary = fbk.feedback_summary(session, days=days)
        summary.pop("by_tag_code", None)
        return {"summary": summary, "records": [fbk.feedback_dict(r) for r in rows]}


@mcp.tool()
@logged
async def shopping_get_requirement_history(
    requirements: Annotated[list[str], Field(min_length=1, max_length=100,
                                             description="Item names, e.g. the pantry items flagged Regular Purchase")],
    default_cadence_days: Annotated[int, Field(ge=1, le=60, description="Assumed interval when there's no history")] = 7,
) -> dict:
    """For each item: when it was last added to the cart (product, quantity, price), the typical interval
    between purchases, whether the user recently removed it because they had enough, the preferred product
    for it, and a due / not_due / unknown verdict. Use this to decide which regular purchases to stage."""
    with session_scope() as session:
        return {"requirements": fbk.requirement_history(session, requirements, default_cadence_days=default_cadence_days)}


def tool_names() -> list[str]:
    return [t.name for t in mcp._tool_manager.list_tools()]  # noqa: SLF001

