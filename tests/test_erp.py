import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.models import Base, FabricRoll, Franchisee, Product


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def seeded(session):
    franchisee = Franchisee(name="Alpine Home Textiles", territory="Zurich", credit_limit=25000)
    product = Product(
        sku="BED-001",
        name="Cotton Sateen Duvet Set - Queen",
        product_line="Cotton Sateen",
        unit_price=87.0,
        fabric_meters_per_unit=2.1,
    )
    roll = FabricRoll(
        roll_code="ROLL-COT-01",
        fabric_type="Cotton Sateen",
        color="Ivory",
        total_length_m=10.0,
        remaining_length_m=10.0,
        is_remnant=False,
    )
    session.add_all([franchisee, product, roll])
    session.commit()
    return {"franchisee": franchisee, "product": product, "roll": roll}


def test_production_run_creates_finished_stock_and_remnant(session, seeded):
    product, roll = seeded["product"], seeded["roll"]
    # 10m roll, 2.1m/unit -> 4 units = 8.4m consumed, 1.6m leftover -> below 2.0m remnant threshold -> scrapped
    run = services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=roll.id)

    assert run.units_produced == 4
    assert run.fabric_consumed_m == pytest.approx(8.4)
    assert run.leftover_m == pytest.approx(1.6)
    assert run.remnant_roll_id is None  # below REMNANT_MIN_LENGTH_M -> scrapped, not kept
    assert run.scrapped_m == pytest.approx(1.6)
    assert services.finished_goods_stock(session, product.id) == 4


def test_production_run_keeps_remnant_above_threshold(session, seeded):
    product, roll = seeded["product"], seeded["roll"]
    # 10m roll, 2.1m/unit -> 3 units = 6.3m consumed, 3.7m leftover -> above 2.0m threshold -> kept as remnant
    run = services.run_production(session, product.id, units_to_produce=3, fabric_roll_id=roll.id)

    assert run.remnant_roll_id is not None
    remnant = session.get(type(roll), run.remnant_roll_id)
    assert remnant.is_remnant is True
    assert remnant.parent_roll_id == roll.id
    assert remnant.remaining_length_m == pytest.approx(3.7)


def test_production_run_rejects_insufficient_fabric(session, seeded):
    product, roll = seeded["product"], seeded["roll"]
    with pytest.raises(services.InsufficientFabricError):
        services.run_production(session, product.id, units_to_produce=100, fabric_roll_id=roll.id)
    # nothing should have changed
    assert services.finished_goods_stock(session, product.id) == 0
    assert roll.remaining_length_m == 10.0


def test_remnant_preferred_over_full_roll(session, seeded):
    product = seeded["product"]
    # Produce once to create a remnant, then produce again with auto-allocation and
    # confirm the second run pulls from the remnant roll, not a fresh full roll.
    first_roll = FabricRoll(
        roll_code="ROLL-COT-02", fabric_type="Cotton Sateen", color="Ivory",
        total_length_m=50.0, remaining_length_m=50.0, is_remnant=False,
    )
    session.add(first_roll)
    session.commit()

    run1 = services.run_production(session, product.id, units_to_produce=1, fabric_roll_id=first_roll.id)
    remnant_id = run1.remnant_roll_id
    assert remnant_id is not None

    run2 = services.run_production(session, product.id, units_to_produce=1)  # auto-allocate
    assert run2.fabric_roll_id == remnant_id


def test_place_order_deducts_stock_and_raises_invoice(session, seeded):
    franchisee, product = seeded["franchisee"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)

    order = services.place_franchise_order(
        session, franchisee.id, [services.OrderLineRequest(product.id, 3)]
    )

    assert order.status.value == "confirmed"
    assert services.finished_goods_stock(session, product.id) == 1  # 4 produced - 3 sold
    ar = services.outstanding_ar(session, franchisee.id)
    assert ar == pytest.approx(3 * product.unit_price)


def test_order_rejected_atomically_on_insufficient_stock(session, seeded):
    franchisee, product = seeded["franchisee"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=2, fabric_roll_id=seeded["roll"].id)

    with pytest.raises(services.InsufficientStockError):
        services.place_franchise_order(
            session, franchisee.id, [services.OrderLineRequest(product.id, 999)]
        )

    # no partial fulfillment: stock untouched, no invoice created
    assert services.finished_goods_stock(session, product.id) == 2
    assert services.outstanding_ar(session, franchisee.id) == 0.0


def test_payment_reduces_outstanding_balance(session, seeded):
    franchisee, product = seeded["franchisee"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)
    order = services.place_franchise_order(
        session, franchisee.id, [services.OrderLineRequest(product.id, 2)]
    )
    from app.models import Invoice
    invoice = session.query(Invoice).filter_by(sales_order_id=order.id).one()

    services.record_payment(session, invoice.id, invoice.amount / 2)
    assert invoice.balance == pytest.approx(invoice.amount / 2)

    with pytest.raises(ValueError):
        services.record_payment(session, invoice.id, invoice.amount)  # overpayment
