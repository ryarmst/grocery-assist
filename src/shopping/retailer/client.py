"""PC Express (No Frills) retailer client.

Only an explicit set of operations is implemented: search, product details, deals, cart
read, cart quantity changes. There is deliberately no generic request method, and the
private transport refuses any path outside `_ALLOWED`, so checkout / order / payment /
timeslot endpoints cannot be reached through this client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from shopping.auth.session import AuthSession
from shopping.retailer.normalize import Cart, NormalizedProduct, cart_from_response, from_product_detail, from_search_tile

log = logging.getLogger(__name__)

# (method, path regex) pairs this client is permitted to call. Nothing else.
_ALLOWED: list[tuple[str, re.Pattern]] = [
    ("POST", re.compile(r"^/v2/products/search$")),
    ("GET", re.compile(r"^/v1/products/[\w-]+$")),
    ("GET", re.compile(r"^/v1/customers/[\w-]+/carts$")),
    ("GET", re.compile(r"^/v1/carts/[\w-]+$")),
    ("POST", re.compile(r"^/v1/carts/[\w-]+$")),  # body restricted to {"entries": ...} below
]

DEAL_FILTERS = {
    "flyer": {"flyers": ["FLYER_ITEM"]},
    "price_reduction": {"promotions": ["Price Reduction"]},
}


class RetailerError(Exception):
    pass


class PCExpressClient:
    MIN_INTERVAL_SECONDS = 0.25  # be polite to the retailer

    def __init__(self, *, api_base: str, api_key: str, banner: str, store_id: str, auth: AuthSession, raw_dir: Path | None = None):
        if not api_key:
            raise RetailerError("PCX_API_KEY is not configured (see .env.example).")
        self._base = api_base.rstrip("/")
        self._banner = banner
        self.store_id = store_id
        self._auth = auth
        self._raw_dir = raw_dir
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cart_id: str | None = None
        self._http = httpx.AsyncClient(
            timeout=30,
            headers={
                "x-apikey": api_key,
                "site-banner": banner,
                "basesiteid": banner,
                "x-loblaw-tenant-id": "ONLINE_GROCERIES",
                "business-user-agent": "PCXWEB",
                "x-application-type": "web",
                "x-channel": "web",
                "accept-language": "en",
                "is-iceberg-enabled": "true",
                "is-helios-account": "false",
                "accept": "application/json",
                "content-type": "application/json",
                "origin": f"https://www.{banner}.ca",
                "referer": f"https://www.{banner}.ca/",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport -------------------------------------------------------------------
    async def _call(self, op: str, method: str, path: str, *, params: dict | None = None, body: dict | None = None, auth: bool = False) -> dict:
        if not any(m == method and rx.match(path) for m, rx in _ALLOWED):
            raise RetailerError(f"Refusing non-allowlisted retailer call {method} {path}")
        headers = await self._auth.get_headers() if auth else {}
        async with self._lock:
            wait = self.MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            started = time.monotonic()
            try:
                resp = await self._http.request(method, self._base + path, params=params, json=body, headers=headers)
            finally:
                self._last_call = time.monotonic()
        elapsed = (time.monotonic() - started) * 1000
        log.info("retailer op=%s %s %s status=%s duration_ms=%.0f", op, method, path, resp.status_code, elapsed)
        if resp.status_code in (401, 403):
            if auth:
                raise RetailerError(f"Retailer rejected the session ({resp.status_code}); paste a fresh AccessToken on /auth.")
            raise RetailerError(f"Retailer refused the request ({resp.status_code}).")
        if resp.status_code >= 400:
            raise RetailerError(f"Retailer error {resp.status_code} for {op}: {resp.text[:200]}")
        data = resp.json()
        self._save_raw(op, data)
        return data

    def _save_raw(self, op: str, data: dict) -> None:
        """Keep the latest raw response per operation for debugging (customer details stripped)."""
        if not self._raw_dir:
            return
        try:
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            clean = data
            if isinstance(data, dict) and ("customer" in data or "cart" in data):
                clean = json.loads(json.dumps(data))
                (clean.get("cart") or clean).pop("customer", None)
            (self._raw_dir / f"{op}.json").write_text(json.dumps(clean))
        except Exception:  # noqa: BLE001 - debugging aid only
            log.debug("could not save raw response for %s", op)

    def _date(self) -> str:
        return datetime.now(ZoneInfo("America/Toronto")).strftime("%d%m%Y")

    # -- catalogue -------------------------------------------------------------------
    async def _search(self, term: str, filters: dict, page: int, page_size: int) -> tuple[list[NormalizedProduct], dict]:
        body = {
            "listingInfo": {"filters": filters, "includeFiltersInResponse": True, "pagination": {"from": page, "size": page_size}, "sort": {}},
            "banner": self._banner,
            "cart": {"cartId": ""},
            "userData": {},
            "fulfillmentInfo": {"offerType": "OG", "storeId": self.store_id, "pickupType": "STORE", "date": self._date(), "timeSlot": None},
            "searchRelatedInfo": {"term": term, "options": []},
            "device": {"screenSize": 1920},
        }
        data = await self._call("search", "POST", "/v2/products/search", body=body)
        products: list[NormalizedProduct] = []
        meta: dict = {}
        sections = (data.get("layout") or {}).get("sections") or {}
        for comp in (sections.get("mainContentCollection") or {}).get("components") or []:
            cdata = comp.get("data") or {}
            for tile in cdata.get("productTiles") or []:
                products.append(from_search_tile(tile))
            if cdata.get("pagination"):
                meta["total_results"] = cdata["pagination"].get("totalResults")
        for comp in (sections.get("filtersSection") or {}).get("components") or []:
            groups = (comp.get("data") or {}).get("filterGroups")
            if groups:
                meta["filter_groups"] = groups
        return products, meta

    async def search_products(self, query: str, *, page: int = 1, include_sponsored: bool = False, filters: dict | None = None) -> dict:
        products, meta = await self._search(query, filters or {}, page, 48)
        if not include_sponsored:
            products = [p for p in products if not p.sponsored]
        # Sponsored placements can duplicate organic results.
        seen: set[str] = set()
        unique = []
        for p in products:
            if p.retailer_product_id not in seen:
                seen.add(p.retailer_product_id)
                unique.append(p)
        return {"products": unique, "total_results": meta.get("total_results"), "filter_groups": meta.get("filter_groups", [])}

    async def get_product(self, product_id: str) -> NormalizedProduct:
        params = {"lang": "en", "date": self._date(), "pickupType": "STORE", "storeId": self.store_id, "banner": self._banner}
        data = await self._call("product", "GET", f"/v1/products/{product_id}", params=params)
        return from_product_detail(data)

    async def get_deals(self, *, query: str = "", kind: str = "flyer", category_code: str | None = None, min_discount_pct: float | None = None, limit: int = 50) -> list[NormalizedProduct]:
        if kind not in DEAL_FILTERS:
            raise RetailerError(f"Unknown deal kind {kind!r}; use one of {sorted(DEAL_FILTERS)}")
        filters = dict(DEAL_FILTERS[kind])
        if category_code:
            filters["category"] = [category_code]
        out: list[NormalizedProduct] = []
        seen: set[str] = set()
        page = 1
        while len(out) < limit and page <= 10:
            products, meta = await self._search(query, filters, page, 48)
            if not products:
                break
            for p in products:
                if p.sponsored or p.retailer_product_id in seen:
                    continue
                seen.add(p.retailer_product_id)
                if min_discount_pct is not None and (p.discount_pct or 0) < min_discount_pct:
                    continue
                out.append(p)
            total = meta.get("total_results") or 0
            if page * 48 >= total:
                break
            page += 1
        return out[:limit]

    # -- cart (authenticated) --------------------------------------------------------
    async def _current_cart_id(self) -> str:
        customer_id = await self._auth.ensure_authenticated()
        if self._cart_id:
            return self._cart_id
        data = await self._call("customer_carts", "GET", f"/v1/customers/{customer_id}/carts", params={"status": "open", "banner": self._banner}, auth=True)
        carts = data.get("carts") or []
        if not carts:
            raise RetailerError("No open cart on the account. Open nofrills.ca once while logged in to create one.")
        self._cart_id = carts[0]["id"]
        return self._cart_id

    async def get_cart(self) -> Cart:
        cart_id = await self._current_cart_id()
        try:
            data = await self._call("cart", "GET", f"/v1/carts/{cart_id}", auth=True)
        except RetailerError:
            self._cart_id = None  # cart may have been replaced; rediscover next time
            raise
        return cart_from_response(data)

    async def set_cart_quantity(self, product_id: str, quantity: float) -> Cart:
        if quantity < 0:
            raise RetailerError("Quantity cannot be negative.")
        cart_id = await self._current_cart_id()
        body = {"entries": {product_id: {"quantity": quantity, "fulfillmentMethod": "pickup", "sellerId": self.store_id}}}
        data = await self._call("cart_update", "POST", f"/v1/carts/{cart_id}", body=body, auth=True)
        errors = data.get("errors") or []
        if errors:
            raise RetailerError(f"Retailer reported cart errors: {json.dumps(errors)[:300]}")
        return cart_from_response(data)

    async def remove_cart_item(self, product_id: str) -> Cart:
        return await self.set_cart_quantity(product_id, 0)
