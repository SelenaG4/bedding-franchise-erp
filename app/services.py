"""Core business logic -- this module is the actual "ERP" part: the place where
production, sales, and accounting stop being three separate systems and become
one atomic set of operations against a shared system of record.

Two operations matter most:

- run_production(): consumes fabric, produces finished-goods stock, and handles
  the leftover fabric explicitly (kept as a remnant roll, or recorded as scrap
  below the usable threshold) instead of losing track of it.
- place_franchise_order(): checks finished-goods stock for every line BEFORE
  touching anything, so an order either fully succeeds (stock deducted + invoice
  raised, atomically) or fully fails (nothing changes, no orphan invoice, no
  partial stock deduction) -- no half-fulfilled order can exist in this system.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    REMNANT_MIN_LENGTH_M,
    FabricRoll,
    FinishedGoodsMovement,
    Invoice,
    OrderStatus,
    Payment,
    Product,
    ProductionRun,
    SalesOrder,
    SalesOrderLine,
)


class InsufficientFabricError(Exception):
    pass


class InsufficientStockError(Exception):
    def __init__(self, shortfalls: dict[int, float]):
        self.shortfalls = shortfalls
        super().__init__(f"Insufficient finished-goods stock for products: {shortfalls}")


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


def place_franchise_order(
    session: Session, franchisee_id: int, lines: list[OrderLineRequest]
) -> SalesOrder:
    """All-or-nothing: check every line's stock before mutating anything, so a
    rejected order leaves zero trace in stock or accounting."""
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
        order = SalesOrder(franchisee_id=franchisee_id, status=OrderStatus.REJECTED_INSUFFICIENT_STOCK)
        session.add(order)
        session.commit()
        raise InsufficientStockError(shortfalls)

    order = SalesOrder(franchisee_id=franchisee_id, status=OrderStatus.CONFIRMED)
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
        session.add(
            FinishedGoodsMovement(
                product_id=line.product_id,
                quantity_delta=-line.quantity,
                reason="franchise order",
                ref_type="sales_order",
                ref_id=order.id,
            )
        )
        total += product.unit_price * line.quantity

    invoice = Invoice(sales_order_id=order.id, amount=total, balance=total)
    session.add(invoice)
    session.commit()
    return order


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


def outstanding_ar(session: Session, franchisee_id: int | None = None) -> float:
    stmt = select(func.coalesce(func.sum(Invoice.balance), 0.0))
    if franchisee_id is not None:
        stmt = stmt.join(SalesOrder, SalesOrder.id == Invoice.sales_order_id).where(
            SalesOrder.franchisee_id == franchisee_id
        )
    return float(session.scalar(stmt) or 0.0)
