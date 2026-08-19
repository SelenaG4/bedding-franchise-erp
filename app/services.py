"""Core business logic -- this module is the actual "ERP" part: the place where
production, sales, and accounting stop being three separate systems and become
one atomic set of operations against a shared system of record.

Three operations matter most:

- run_production(): consumes fabric, produces finished-goods stock, and handles
  the leftover fabric explicitly (kept as a remnant roll, or recorded as scrap
  below the usable threshold) instead of losing track of it.
- place_order(): checks finished-goods stock for every line BEFORE touching
  anything, so an order either fully succeeds (stock deducted + invoice raised,
  atomically) or fully fails (nothing changes, no orphan invoice, no partial
  stock deduction) -- no half-fulfilled order can exist in this system. It also
  enforces each sales channel's own checkout procedure (see CHANNEL_POLICIES
  below) before an order is even created.
- CHANNEL_POLICIES: the six sales channels a real bedding manufacturer ships
  through don't check out the same way. A supermarket partner needs a PO
  number on file before an order is accepted; an international order needs a
  customs declaration; a company-owned retail shop isn't really a "sale" at
  all -- it's an internal stock transfer, so it skips invoicing entirely.
  Packaging differs the same way: a single online order goes out in a poly
  mailer, a supermarket pallet needs GS1 compliance labels, an export order
  needs an export crate and a commercial invoice.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    REMNANT_MIN_LENGTH_M,
    Account,
    FabricRoll,
    FinishedGoodsMovement,
    Invoice,
    OrderStatus,
    Payment,
    Product,
    ProductionRun,
    SalesChannel,
    SalesOrder,
    SalesOrderLine,
)


class InsufficientFabricError(Exception):
    pass


class InsufficientStockError(Exception):
    def __init__(self, shortfalls: dict[int, float]):
        self.shortfalls = shortfalls
        super().__init__(f"Insufficient finished-goods stock for products: {shortfalls}")


class ChannelRequirementError(Exception):
    """Raised when an order is missing paperwork its channel requires (a PO
    number, a customs declaration) -- rejected before any DB row is created,
    since this is a data-entry problem, not a fulfillment-capacity one."""

    pass


@dataclass(frozen=True)
class ChannelPolicy:
    payment_terms: str  # "prepaid" | "net_30" | "net_60" | "internal_transfer"
    packaging: str
    requires_po_number: bool = False
    requires_customs_docs: bool = False
    raises_invoice: bool = True


CHANNEL_POLICIES: dict[SalesChannel, ChannelPolicy] = {
    SalesChannel.INDIVIDUAL: ChannelPolicy(
        payment_terms="prepaid",
        packaging="single-item retail-ready poly mailer",
    ),
    SalesChannel.ONLINE_WAREHOUSE: ChannelPolicy(
        payment_terms="prepaid",
        packaging="carton, SKU-barcoded per unit for pick-and-pack fulfillment",
    ),
    SalesChannel.COMPANY_RETAIL: ChannelPolicy(
        payment_terms="internal_transfer",
        packaging="shelf-ready carton, store-display packaging",
        raises_invoice=False,  # moving stock within the company, not a sale
    ),
    SalesChannel.SUPERMARKET_PARTNER: ChannelPolicy(
        payment_terms="net_60",
        packaging="palletized, GS1-compliant shelf labeling",
        requires_po_number=True,
    ),
    SalesChannel.FRANCHISEE: ChannelPolicy(
        payment_terms="net_30",
        packaging="standard carton with franchise branding insert",
    ),
    SalesChannel.INTERNATIONAL: ChannelPolicy(
        payment_terms="net_30",
        packaging="export crate, customs-compliant labeling + commercial invoice",
        requires_customs_docs=True,
    ),
}


def finished_goods_stock(session: Session, product_id: int) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(FinishedGoodsMovement.quantity_delta), 0)).where(
            FinishedGoodsMovement.product_id == product_id
        )
    )
    return int(total or 0)


def allocate_fabric_roll(session: Session, product: Product, units_needed: int) -> FabricRoll:
    """Best-fit allocation, remnants preferred: pick the smallest roll (remnant
    rolls first) that still has enough length to cover this run. This is what
    actually reduces waste over time -- burning down remnants before cutting into
    fresh full rolls -- rather than always pulling from the newest roll."""
    meters_needed = product.fabric_meters_per_unit * units_needed

    candidates = session.scalars(
        select(FabricRoll)
        .where(FabricRoll.fabric_type == product.product_line)
        .where(FabricRoll.remaining_length_m >= meters_needed)
        .order_by(FabricRoll.is_remnant.desc(), FabricRoll.remaining_length_m.asc())
    ).all()

    if not candidates:
        raise InsufficientFabricError(
            f"No fabric roll with >= {meters_needed:.1f}m of '{product.product_line}' available"
        )
    return candidates[0]


def run_production(
    session: Session, product_id: int, units_to_produce: int, fabric_roll_id: int | None = None
) -> ProductionRun:
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError(f"Unknown product_id {product_id}")

    roll = (
        session.get(FabricRoll, fabric_roll_id)
        if fabric_roll_id is not None
        else allocate_fabric_roll(session, product, units_to_produce)
    )
    if roll is None:
        raise ValueError(f"Unknown fabric_roll_id {fabric_roll_id}")

    meters_needed = product.fabric_meters_per_unit * units_to_produce
    if roll.remaining_length_m < meters_needed:
        raise InsufficientFabricError(
            f"Roll {roll.roll_code} has {roll.remaining_length_m:.1f}m, needs {meters_needed:.1f}m"
        )

    leftover = roll.remaining_length_m - meters_needed
    roll.remaining_length_m = 0.0  # this roll is fully consumed by this run

    run = ProductionRun(
        product_id=product_id,
        fabric_roll_id=roll.id,
        units_produced=units_to_produce,
        fabric_consumed_m=meters_needed,
        leftover_m=leftover,
    )
    session.add(run)
    session.flush()  # get run.id before referencing it

    if leftover >= REMNANT_MIN_LENGTH_M:
        remnant = FabricRoll(
            roll_code=f"{roll.roll_code}-R{run.id}",
            fabric_type=roll.fabric_type,
            color=roll.color,
            total_length_m=leftover,
            remaining_length_m=leftover,
            is_remnant=True,
            parent_roll_id=roll.id,
        )
        session.add(remnant)
        session.flush()
        run.remnant_roll_id = remnant.id
    else:
        run.scrapped_m = leftover

    session.add(
        FinishedGoodsMovement(
            product_id=product_id,
            quantity_delta=units_to_produce,
            reason="production run",
            ref_type="production_run",
            ref_id=run.id,
        )
    )
    session.commit()
    return run


@dataclass
class OrderLineRequest:
    product_id: int
    quantity: int


def place_order(
    session: Session,
    account_id: int,
    lines: list[OrderLineRequest],
    po_number: str | None = None,
    customs_declaration: str | None = None,
) -> SalesOrder:
    """All-or-nothing: check every line's stock before mutating anything, so a
    rejected order leaves zero trace in stock or accounting. Before that, checks
    the order carries whatever paperwork its channel requires -- a missing PO
    number or customs declaration is rejected outright, with no order row
    created at all (this is a data problem, not a stock-availability one)."""
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Unknown account_id {account_id}")

    policy = CHANNEL_POLICIES[account.channel]
    if policy.requires_po_number and not po_number:
        raise ChannelRequirementError(
            f"{account.channel.value} orders require a PO number on file before they can be accepted"
        )
    if policy.requires_customs_docs and not customs_declaration:
        raise ChannelRequirementError(
            f"{account.channel.value} orders require a customs declaration before they can be accepted"
        )

    shortfalls: dict[int, float] = {}
    products: dict[int, Product] = {}
    for line in lines:
        product = session.get(Product, line.product_id)
        if product is None:
            raise ValueError(f"Unknown product_id {line.product_id}")
        products[line.product_id] = product
        available = finished_goods_stock(session, line.product_id)
        if available < line.quantity:
            shortfalls[line.product_id] = available - line.quantity  # negative = short by this much

    if shortfalls:
        order = SalesOrder(
            account_id=account_id,
            channel=account.channel,
            status=OrderStatus.REJECTED_INSUFFICIENT_STOCK,
            packaging=policy.packaging,
            po_number=po_number,
            customs_declaration=customs_declaration,
        )
        session.add(order)
        session.commit()
        raise InsufficientStockError(shortfalls)

    order = SalesOrder(
        account_id=account_id,
        channel=account.channel,
        status=OrderStatus.CONFIRMED,
        packaging=policy.packaging,
        po_number=po_number,
        customs_declaration=customs_declaration,
    )
    session.add(order)
    session.flush()

    total = 0.0
    for line in lines:
        product = products[line.product_id]
        session.add(
            SalesOrderLine(
                sales_order_id=order.id,
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=product.unit_price,
            )
        )
        reason = "internal stock transfer" if not policy.raises_invoice else f"{account.channel.value} order"
        session.add(
            FinishedGoodsMovement(
                product_id=line.product_id,
                quantity_delta=-line.quantity,
                reason=reason,
                ref_type="sales_order",
                ref_id=order.id,
            )
        )
        total += product.unit_price * line.quantity

    order.total_value = total

    if policy.raises_invoice:
        invoice = Invoice(sales_order_id=order.id, amount=total, balance=total)
        session.add(invoice)

    session.commit()
    return order


# Backward-compatible alias -- the original name from when this system only
# handled franchisee orders.
def place_franchise_order(
    session: Session, franchisee_id: int, lines: list[OrderLineRequest]
) -> SalesOrder:
    return place_order(session, franchisee_id, lines)


def record_payment(session: Session, invoice_id: int, amount: float) -> Invoice:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise ValueError(f"Unknown invoice_id {invoice_id}")
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    if amount > invoice.balance + 1e-6:
        raise ValueError(f"Payment {amount} exceeds outstanding balance {invoice.balance}")

    session.add(Payment(invoice_id=invoice_id, amount=amount))
    invoice.balance -= amount
    session.commit()
    return invoice


def remnant_inventory_summary(session: Session) -> list[dict]:
    rows = session.execute(
        select(FabricRoll.fabric_type, FabricRoll.color, func.sum(FabricRoll.remaining_length_m))
        .where(FabricRoll.is_remnant.is_(True))
        .where(FabricRoll.remaining_length_m > 0)
        .group_by(FabricRoll.fabric_type, FabricRoll.color)
    ).all()
    return [
        {"fabric_type": r[0], "color": r[1], "remnant_meters_available": float(r[2])} for r in rows
    ]


def outstanding_ar(session: Session, account_id: int | None = None) -> float:
    stmt = select(func.coalesce(func.sum(Invoice.balance), 0.0))
    if account_id is not None:
        stmt = stmt.join(SalesOrder, SalesOrder.id == Invoice.sales_order_id).where(
            SalesOrder.account_id == account_id
        )
    return float(session.scalar(stmt) or 0.0)


def sales_by_channel(session: Session) -> list[dict]:
    """Cross-channel reporting -- total order value and count per channel,
    the view that shows why individual/company-retail orders (no invoice)
    still need to be visible even though they don't show up in AR."""
    rows = session.execute(
        select(SalesOrder.channel, func.count(SalesOrder.id), func.coalesce(func.sum(SalesOrder.total_value), 0.0))
        .where(SalesOrder.status == OrderStatus.CONFIRMED)
        .group_by(SalesOrder.channel)
    ).all()
    return [{"channel": r[0].value, "order_count": r[1], "total_value": float(r[2])} for r in rows]


def orders_for_account(session: Session, account_id: int) -> list[SalesOrder]:
    """An account's own order history -- what app.auth's role-based access
    check exists to protect: without this endpoint there was previously no
    way to list an account's orders at all, at any permission level."""
    return list(
        session.scalars(
            select(SalesOrder).where(SalesOrder.account_id == account_id).order_by(SalesOrder.order_date.desc())
        ).all()
    )


def get_or_create_individual_account(session: Session, contact_email: str, name: str, location: str) -> Account:
    """The manual stand-in for a POS integration: looks up an existing
    individual-channel account by contact_email, or creates one, so a repeat
    walk-in customer gets their own persistent account (and their own AR/order
    history) instead of everyone sharing one bucket account.

    Scoped to channel == INDIVIDUAL on purpose -- a contact_email that already
    belongs to, say, a franchisee's account shouldn't collide with a walk-in
    account for the same person; those are legitimately different accounts.
    A real POS integration would call this (or an equivalent) at checkout
    time instead of a human doing it through this function or the
    /accounts/individual/lookup-or-create endpoint.
    """
    existing = session.scalars(
        select(Account)
        .where(Account.channel == SalesChannel.INDIVIDUAL)
        .where(Account.contact_email == contact_email)
    ).first()
    if existing is not None:
        return existing

    account = Account(
        name=name,
        channel=SalesChannel.INDIVIDUAL,
        location=location,
        contact_email=contact_email,
    )
    session.add(account)
    session.commit()
    return account
