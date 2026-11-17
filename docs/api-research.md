# PC Express / No Frills API research

Observed 2026-11-03 from a browser HAR capture with a dedicated test account, then replayed
from a plain Python client (`httpx`/`urllib`). Sanitized examples are in `tests/fixtures/`.
No checkout, order, payment or timeslot endpoint was called.

## Hosts and bot protection

| Host | Purpose | Non-browser client? |
|---|---|---|
| `www.nofrills.ca` | Next.js site (SSR pages, `/_next/data/...json`) | **Blocked** (Akamai, HTTP 403 "Access Denied") |
| `accounts.pcid.ca` | Login / OAuth (Oracle IDCS) | **Blocked**, including `POST /oauth2/v1/token` (refresh) |
| `api.pcexpress.ca/pcx-bff/api` | Product, search, cart API ("BFF") | **Works** with an API key + static headers |

Consequence: the application can call the BFF directly, but it cannot log in or refresh
tokens. A logged-in browser must supply the access token (see Authentication). We do not
try to work around the bot protection.

## Common request headers (BFF)

```
x-apikey: <public 32-char key embedded in the site's JavaScript>   (configured as PCX_API_KEY)
site-banner: nofrills
basesiteid: nofrills
x-loblaw-tenant-id: ONLINE_GROCERIES
business-user-agent: PCXWEB
x-application-type: web
x-channel: web
is-iceberg-enabled: true
is-helios-account: false            (true when authenticated)
content-type: application/json
accept-language: en
authorization: Bearer <AccessToken> (authenticated calls only)
```

No cookies are needed for BFF calls. Missing `x-apikey` → 401.

## Authentication

- OAuth 2 authorization code + PKCE against `https://accounts.pcid.ca/oauth2/v1/authorize`
  (`client_id=48bb3047…`, `scope=openid grocery-customer offline_access`, redirect to
  `https://www.nofrills.ca/login/redirect`). The site's JS exchanges the code at
  `/oauth2/v1/token` with HTTP Basic client authentication.
- Token response: `access_token` (JWT, **3600 s**), `refresh_token` (opaque, ~164 chars),
  `id_token` (JWT, ~2 h), `expires_in: 3600`. Session idle limit claim `sidle=480` (minutes).
- "Keep me signed in" sets `cookieKmsiToken` on `accounts.pcid.ca` for 180 days, so the
  browser can silently re-authenticate.
- After login the site calls `POST /v1/ecommerce/v2/customers/update-token {"idToken": ...}`
  and `POST accounts.pcid.ca/v1/auth/session`.
- **Storage:** the site writes `AccessToken` and `RefreshToken` as ordinary (non-HttpOnly)
  cookies on `www.nofrills.ca`. These are what a user copies into this app's `/auth` page.
- Customer id (`pcid`) = JWT `sub` claim; used in `/v1/customers/{pcid}/...` paths.
- Logout was not captured.

Fixture: `auth_metadata.json` (structure and claim names only).

## Product search — `POST /v2/products/search`

```json
{
  "listingInfo": {"filters": {}, "includeFiltersInResponse": true,
                  "pagination": {"from": 1, "size": 48}, "sort": {}},
  "banner": "nofrills",
  "cart": {"cartId": ""},
  "userData": {},
  "fulfillmentInfo": {"offerType": "OG", "storeId": "3728", "pickupType": "STORE",
                      "date": "03112026", "timeSlot": null},
  "searchRelatedInfo": {"term": "peanut butter", "options": []},
  "device": {"screenSize": 1920}
}
```

- `pagination.from` is a **page number** (1-based), `size` max observed 48.
- Response is a server-driven UI layout. Products are in
  `layout.sections.mainContentCollection.components[*].data.productTiles[]` (several grid
  components per page, ~18 tiles each) and `...data.pagination.totalResults`.
- Facets are in `layout.sections.filtersSection.components[0].data.filterGroups[]`:
  `category`, `productBrand`, `promotions`, `price ranges`, `shopCanadian`, `flyers`,
  `dietaryCallouts`. Filter values must be **lists**: `{"productBrand": ["KRFT"]}`.
- An empty `term` searches the whole catalogue (~13.6k products).
- Sponsored tiles (`isSponsored: true`, with `sponsoredCreative`) are interleaved and can
  duplicate organic results; we drop them.
- Type-ahead: `POST /v1/products/type-ahead {"term", "size", "storeId", ...}` → suggestions only.

### Tile fields used

`productId` (e.g. `20039581001_EA`, `20654851_KG`), `articleNumber`, `uom` (`EA`, `KG`,
`C12` for 12-packs…), `brand`, `title`, `packageSizing`, `pricing.price` (string, current
price), `pricing.wasPrice` ("$6.29" when on sale), `pricing.memberOnlyPrice`,
`pricingUnits.{type, unit, interval, minOrderQuantity, maxOrderQuantity, weighted}`, `deal`,
`pcoDeal`, `mopDeal`, `promotions[]`, `inventoryIndicator.text` ("Out of Stock"),
`productImage[].smallUrl`, `link`.

### `packageSizing` formats

| Pattern | Example | Notes |
|---|---|---|
| `N g/kg, $X/100g` | `1 kg, $0.60/100g` | most common |
| `N l/ml, $X/100ml` | `4 l, $0.16/100ml` | |
| `NxM ml, $X/100ml` | `12x355.0 ml, $0.20/100ml` | multipack |
| `N ea, $X/1ea` / `NxM ea` | `3x98.0 ea, $2.33/1ea` | count; retailer's per-ea basis is inconsistent |
| `$X/1kg $Y/1lb` | `$15.41/1kg $6.99/1lb` | `SOLD_BY_EACH_PRICED_BY_WEIGHT`: price is an estimate for an average pack |
| `$X/1kg` with `SOLD_BY_WEIGHT` | Okra `0.66`, interval 100 g | price per selling interval |

The retailer's unit price is computed from the **regular** price; ours uses the effective
(sale) price.

## Deals and promotions

Exposed as search facets, not a separate endpoint:

- `filters: {"flyers": ["FLYER_ITEM"]}` → weekly flyer items (1,807 at store 3640).
- `filters: {"promotions": ["Price Reduction"]}` → all reduced items (1,962). Other values
  such as `"$1,$2,$3,$4,$5"` (fixed-price buckets) also appear.
- Combine with `term` and `category` to narrow.

Deal representations on a tile:

| Field | Shape | Example |
|---|---|---|
| `deal` | `{type, text, expiryDate, dealPrice, points}` | `SALE` "SAVE $0.32"; `LIMIT` "Limit 6, after limit $3.00" (dealPrice 1.88) |
| `pcoDeal` | string | "250 PC Optimum Points", "PC Optimum Offer" |
| `mopDeal` / `pricing.memberOnlyPrice` | member-only price | none observed at this store |
| `promotions[]` | `{type:"DEAL", promoCode, text:"PC Optimum Points", expiryDate, callToAction}` | links to `/shop-by-promo/{promoCode}` |

`filters: {"promotionCodes": [code]}` lists products in one promotion (used by the site's
"shop by promo" drawer). Personal PC Optimum offers: `GET /v1/ecommerce/v2/nofrills/customers/promotions`
(authenticated, not used yet). The flyer viewer itself comes from Flipp (`cdn.flippenterprise.net`).

## Product details — `GET /v1/products/{code}`

Query: `lang=en&date=DDMMYYYY&pickupType=STORE&storeId=3728&banner=nofrills`. Anonymous.

Adds `gtin` (UPC), `packageSize` ("1 kg"), `averageWeight`, `breadcrumbs`, `nutritionFacts`,
`ingredients`, `variants`, and `offers[0]`:
`price {value, unit, type: SPECIAL|..., expiryDate}`, `wasPrice`, `comparisonPrices[]
{value, unit, quantity}`, `memberOnlyPrice`, `promotions`, `stockStatus` ("OK").

Fixture: `product_detail.json`.

## Cart

All cart calls need `authorization: Bearer <AccessToken>` + `is-helios-account: true`.

| Operation | Request |
|---|---|
| Find open cart | `GET /v1/customers/{pcid}/carts?status=open&banner=nofrills` → `{"carts":[{"id","modifiedTime"}]}` |
| Read cart | `GET /v1/carts/{cartId}` (optional `?inventory=true`) |
| Set quantity | `POST /v1/carts/{cartId}` `{"entries": {"<productId>": {"quantity": 2, "fulfillmentMethod": "pickup", "sellerId": "<storeId>"}}}` |
| Remove | same, `quantity: 0` |

- Quantity is **absolute** (set, not increment). Several products can be sent in one call.
- **Substitutions (per item)** use the same call; the entry may also carry
  `"substitutionAllowed": true|false`, `"substitutionComment": "<note to shopper>"` and
  `"selectedSubstitution": "Customer Substitute: <brand>, <name>, Price: <price>, ArticleID: <code>"`
  (empty string for none). The cart then reports `substitutionPreference: {substitutionAllowed, comment}` and
  `selectedSubstitution: "<code>"` per entry; orders report `substitutionData` per line.
  `POST /v1/recommendation/substitution-details {bookingDate, productCodes, pcId, cartId, storeId, banner,
  pickupType, timeSlot, lang}` returns the site's suggested substitutes per product (needs a booked slot;
  not used by the app).
- **Submitted orders** can be edited until their cutoff: `GET /v1/customers/{pcid}/carts?status=open` then
  lists the order's cart under `liveOrders` (with `carts: []`). The app deliberately uses only `carts`, so it
  never changes a placed order.
- Response: `{"cart": {...}, "errors": []}`.
- Cart shape: `orders[0].entries[]` with `quantity`, `prices.{salePrice, totalSalePrice,
  totalRegularPrice, comparisonPrices}`, `offer.{id, sellingType, wasPrice, vendor.id,
  product.{id, name, brand}}`; `orders[0].totals.{subTotal, totalDiscounts, estimatedTotal}`;
  `orders[0].fulfillment.pickupBooking.pickupLocation.{id, name, storeId}`.
- Changing the pickup store: `POST /v1/carts/{id}` with `{"booking": {"pickupLocationId": "3728", "postalCode": ""}, "fulfillmentType": "PICKUP"}` (preceded by `/dry-run`). Not used by this app.
- Anonymous carts work without auth by cart id (`GET/POST /v1/carts/{anonId}`). Creation
  of an anonymous cart was not captured.

Fixtures: `cart_empty.json`, `cart_populated.json`, `cart_update.json`.

## Order history (read-only)

Authenticated GETs (bearer token as for the cart):

| Operation | Request |
|---|---|
| Current/live orders | `GET /v1/ecommerce/v2/nofrills/customers/orders?status=SUBMITTED,READY_FOR_ACTION,READY_FOR_PICK_UP,COMPLETED` → `{"orders": [...]}` |
| Past orders | `GET /v1/ecommerce/v2/nofrills/customers/historical-orders` → `{onlineOrdersCount, offlineOrdersCount, orderHistory: [{id, placed, total, store, fulfillmentType, orderType}]}` (all banners: No Frills, Loblaws, Superstore…) |
| One past order | `GET /v1/ecommerce/v2/nofrills/customers/historical-orders/{orderNumber}` (~80 KB) |

Order detail: top level `pointsEarned`, `pointsRedeemed`, `orderDetails`. `orderDetails`:
`orderNumber`, `subTotal`, `totalTax`, `totalPriceWithTax`, `totalItems`, `entries[]`, plus personal data
(`shippingAddress`, `user`, `paymentInfo`, `booking.pickupLocation`, `comment`) that the app never stores.
Each entry: `quantity`, `weight` (kg, for weighed items, with `quantity` 0), `unitPrice` (per kg when
weighed), `totalPrice`, `totalPriceWithoutDiscount`, and `product` (`id`, `productName`, `brand`,
`productTitleSizeDetail`, `isWeighted`, `primaryImage`, `link`). Quirks: unfulfilled items have quantity 0,
weight 0 and total 0; a few billed lines report quantity 0 (infer from total ÷ unit price); delivery fee,
fee waiver and tip appear as pseudo-products (`PD ORDER`, `WAIVE PD ORDER`, `$3 DRIVER TIP`).
The order page on the website is `https://www.nofrills.ca/en/order-dashboard/past-order/{orderNumber}`.

## Cart merge

On login the site:

1. `GET /v1/customers/{pcid}/carts?status=open` → the account's cart id.
2. `POST /v1/carts/{accountCartId}/merge {"sourceCartId": "<anonymous cart id>"}`.

Observed result: the anonymous cart's entries (peanut butter ×2, corn ×1) appeared in the
account cart. Not exposed by this app (it only uses the account cart); it could become a
reviewable operation later.

## Not used / deliberately excluded

`/v1/fulfillment/next-available-plan-timeslots`, `/v1/customers/{pcid}/payment-cards`,
`/v1/customers/{pcid}/shipping-addresses`, `/v2/slug/my-pcx-cart-page`, anything under
checkout or order submission. The client's path allow-list (`retailer/client.py`) rejects
all of them.
