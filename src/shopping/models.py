"""SQLAlchemy models. One SQLite database holds products, prices, runs, preferences and cart history."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class DecimalText(TypeDecorator):
    """Exact Decimal storage (SQLite NUMERIC would round-trip through float)."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(str(value)))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator):
    """Store naive UTC in SQLite, always hand back timezone-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class JSONList(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(list(value or []))

    def process_result_value(self, value, dialect):
        return json.loads(value) if value else []


class Base(DeclarativeBase):
    pass


# Run lifecycle
RUN_BUILDING, RUN_REVIEW, RUN_PARTIAL, RUN_APPROVED, RUN_APPLIED, RUN_CANCELLED = (
    "building", "review", "partially_approved", "approved", "applied", "cancelled",
)
# Selection lifecycle
SEL_PENDING, SEL_APPROVED, SEL_REJECTED, SEL_APPLIED, SEL_FAILED = "pending", "approved", "rejected", "applied", "failed"

REASON_CODES = {
    "grocery_list": "Grocery List",
    "pantry_low": "Pantry Low",
    "regular_staple": "Regular Staple",
    "good_deal": "Good Deal",
    "preferred_product": "Preferred Product",
    "preferred_brand": "Preferred Brand",
    "user_requested": "User Requested",
    "alternative": "Alternative",
}

PREFERENCE_KINDS = {
    "prefer_product": "Prefer this exact product",
    "prefer_brand": "Prefer this brand",
    "prefer_for_type": "Prefer this option for this type of item",
    "blacklist_product": "Don't suggest this product",
    "blacklist_brand": "Don't suggest this brand",
    "prefer_cheaper_equivalent": "Prefer the cheaper equivalent",
    "prefer_larger_packages": "Prefer larger packages",
    "prefer_smaller_packages": "Prefer smaller packages",
    "preference_note": "Free-form note",
}


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer_product_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    article_number: Mapped[str | None] = mapped_column(String(32))
    uom: Mapped[str | None] = mapped_column(String(16))
    gtin: Mapped[str | None] = mapped_column(String(128))
    brand: Mapped[str | None] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    raw_size: Mapped[str | None] = mapped_column(String(64))
    raw_package_sizing: Mapped[str | None] = mapped_column(String(128))
    size_value: Mapped[Decimal | None] = mapped_column(DecimalText)
    size_unit: Mapped[str | None] = mapped_column(String(8))
    size_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    comparison_family: Mapped[str] = mapped_column(String(16), default="unknown")
    sold_by: Mapped[str] = mapped_column(String(32), default="each")
    image_url: Mapped[str | None] = mapped_column(String(512))
    link: Mapped[str | None] = mapped_column(String(512))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    observations: Mapped[list[PriceObservation]] = relationship(back_populates="product", order_by="PriceObservation.observed_at")


class PriceObservation(Base):
    __tablename__ = "price_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    shopping_run_id: Mapped[str | None] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    store_id: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))  # search | deals | product | selection | cart
    regular_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    sale_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    effective_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    member_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    raw_package_size: Mapped[str | None] = mapped_column(String(64))
    raw_package_sizing: Mapped[str | None] = mapped_column(String(128))
    normalized_quantity: Mapped[Decimal | None] = mapped_column(DecimalText)
    normalized_unit: Mapped[str | None] = mapped_column(String(8))
    size_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    unit_price_basis: Mapped[str | None] = mapped_column(String(16))
    retailer_unit_price: Mapped[str | None] = mapped_column(String(64))
    deal_type: Mapped[str | None] = mapped_column(String(32))
    deal_text: Mapped[str | None] = mapped_column(String(128))
    deal_expiry: Mapped[str | None] = mapped_column(String(32))
    pc_optimum_text: Mapped[str | None] = mapped_column(String(128))
    promotion_text: Mapped[str | None] = mapped_column(String(256))
    availability: Mapped[str | None] = mapped_column(String(64))

    product: Mapped[Product] = relationship(back_populates="observations")


class ShoppingRun(Base):
    __tablename__ = "shopping_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(24), default=RUN_BUILDING)
    store_id: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(String(256), default="")
    notes: Mapped[str | None] = mapped_column(Text)

    selections: Mapped[list[Selection]] = relationship(back_populates="run", order_by="Selection.position")
    choice_groups: Mapped[list[ChoiceGroup]] = relationship(back_populates="run", order_by="ChoiceGroup.position")


class ChoiceGroup(Base):
    __tablename__ = "choice_groups"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shopping_run_id: Mapped[str] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    label: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    recommended_selection_id: Mapped[str | None] = mapped_column(String(16))
    selected_selection_id: Mapped[str | None] = mapped_column(String(16))

    run: Mapped[ShoppingRun] = relationship(back_populates="choice_groups")
    selections: Mapped[list[Selection]] = relationship(back_populates="choice_group", order_by="Selection.position")


class Selection(Base):
    __tablename__ = "selections"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shopping_run_id: Mapped[str] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    choice_group_id: Mapped[str | None] = mapped_column(ForeignKey("choice_groups.id"), index=True)
    price_observation_id: Mapped[int | None] = mapped_column(ForeignKey("price_observations.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    position: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float, default=1)
    suggested_quantity: Mapped[float | None] = mapped_column(Float)  # what the agent proposed, before user edits
    added_by: Mapped[str] = mapped_column(String(16), default="agent")  # agent | user
    status: Mapped[str] = mapped_column(String(16), default=SEL_PENDING, index=True)
    requirement: Mapped[str | None] = mapped_column(String(128))
    rationale: Mapped[str | None] = mapped_column(Text)
    reason_codes: Mapped[list] = mapped_column(JSONList, default=list)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    applied_to_cart_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ShoppingRun] = relationship(back_populates="selections")
    product: Mapped[Product] = relationship()
    choice_group: Mapped[ChoiceGroup | None] = relationship(back_populates="selections")
    observation: Mapped[PriceObservation | None] = relationship()


class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    scope: Mapped[str | None] = mapped_column(String(128))  # item type, e.g. "peanut butter"
    target: Mapped[str | None] = mapped_column(String(128))  # retailer product id or brand
    target_label: Mapped[str | None] = mapped_column(String(256))  # human-readable target
    category: Mapped[str | None] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    note: Mapped[str | None] = mapped_column(Text)
    source_selection_id: Mapped[str | None] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CartAction(Base):
    __tablename__ = "cart_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopping_run_id: Mapped[str | None] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    selection_id: Mapped[str | None] = mapped_column(ForeignKey("selections.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    retailer_product_id: Mapped[str] = mapped_column(String(64))
    product_label: Mapped[str | None] = mapped_column(String(256))  # brand + name + size at the time
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(32))  # set_quantity | remove
    source: Mapped[str | None] = mapped_column(String(32))  # approve | apply | cart_page | modify | mcp
    quantity_before: Mapped[float | None] = mapped_column(Float)
    quantity_after: Mapped[float | None] = mapped_column(Float)
    price_at_action: Mapped[Decimal | None] = mapped_column(DecimalText)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    upstream_reference: Mapped[str | None] = mapped_column(String(64))  # retailer cart id


# Quick reason chips shown when triaging. Free text is always allowed alongside.
FEEDBACK_TAGS = {
    "removed": {
        "have_enough": "Have enough / not needed",
        "too_expensive": "Too expensive",
        "not_a_deal": "Not a real deal",
        "wrong_size": "Wrong size",
        "wrong_item": "Not what I meant",
        "dislike_product": "Don't like this product",
        "dislike_brand": "Don't like this brand",
        "quality": "Quality concern",
        "buy_elsewhere": "Buy at Costco / elsewhere",
    },
    "chose_alternative": {
        "cheaper": "Cheaper",
        "better_unit_price": "Better unit price",
        "better_size": "Better size",
        "prefer_brand": "Preferred brand",
        "prefer_product": "Preferred product",
        "better_quality": "Better quality",
        "healthier": "Healthier / ingredients",
    },
    "approved": {
        "good_pick": "Good pick",
        "good_deal": "Good deal",
    },
}
FEEDBACK_TAG_LABELS = {k: v for tags in FEEDBACK_TAGS.values() for k, v in tags.items()} | {"other": "Other"}


class Feedback(Base):
    """A triage decision with the user's reason, kept for later pattern review."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    shopping_run_id: Mapped[str | None] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    selection_id: Mapped[str | None] = mapped_column(ForeignKey("selections.id"), index=True)
    choice_group_id: Mapped[str | None] = mapped_column(String(16))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)  # the item decided on
    chosen_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))  # for chose_alternative
    requirement: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)  # removed | chose_alternative | approved | quantity_changed | note
    tags: Mapped[list] = mapped_column(JSONList, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)  # JSON snapshot: prices, agent reasons, options
    preference_id: Mapped[int | None] = mapped_column(ForeignKey("preferences.id"))  # rule created alongside, if any
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution: Mapped[str | None] = mapped_column(Text)  # what was done about it when reviewed

    product: Mapped[Product | None] = relationship(foreign_keys=[product_id])
    chosen_product: Mapped[Product | None] = relationship(foreign_keys=[chosen_product_id])


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
