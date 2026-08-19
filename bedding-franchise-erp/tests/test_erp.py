import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.models import Account, Base, FabricRoll, Product, SalesChannel


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
    franchisee = Account(
        name="Alpine Home Textiles", channel=SalesChannel.FRANCHISEE, location="Zurich", credit_limit=25000
    )
    company_retail = Account(name="Company Flagship Store", channel=SalesChannel.COMPANY_RETAIL, location="Zurich Oerlikon")
    supermarket = Account(
        name="Migros Distribution AG", channel=SalesChannel.SUPERMARKET_PARTNER, location="Zurich", credit_limit=50000
    )
    international = Account(
        name="Nordic Home Textiles AS", channel=SalesChannel.INTERNATIONAL, location="Oslo, Norway", credit_limit=30000
    )
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
    session.add_all([franchisee, company_retail, supermarket, international, product, roll])
    session.commit()
    return {
        "franchisee": franchisee,
        "company_retail": company_retail,
        "supermarket": supermarket,
        "international": international,
        "product": product,
        "roll": roll,
    }


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


def test_franchisee_order_deducts_stock_and_raises_invoice(session, seeded):
    franchisee, product = seeded["franchisee"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)

    order = services.place_order(
        session, franchisee.id, [services.OrderLineRequest(product.id, 3)]
    )

    assert order.status.value == "confirmed"
    assert order.channel == SalesChannel.FRANCHISEE
    assert order.packaging == "standard carton with franchise branding insert"
    assert services.finished_goods_stock(session, product.id) == 1  # 4 produced - 3 sold
    ar = services.outstanding_ar(session, franchisee.id)
    assert ar == pytest.approx(3 * product.unit_price)


def test_order_rejected_atomically_on_insufficient_stock(session, seeded):
    franchisee, product = seeded["franchisee"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=2, fabric_roll_id=seeded["roll"].id)

    with pytest.raises(services.InsufficientStockError):
        services.place_order(
            session, franchisee.id, [services.OrderLineRequest(product.id, 999)]
        )

    # no partial fulfillment: stock untouched, no invoice created
    assert services.finished_goods_stock(session, product.id) == 2
    assert services.outstanding_ar(session, franchisee.id) == 0.0


def test_payment_reduces_outstanding_balance(session, seeded):
    franchisee, product = seeded["franchisee"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)
    order = services.place_order(
        session, franchisee.id, [services.OrderLineRequest(product.id, 2)]
    )
    from app.models import Invoice
    invoice = session.query(Invoice).filter_by(sales_order_id=order.id).one()

    services.record_payment(session, invoice.id, invoice.amount / 2)
    assert invoice.balance == pytest.approx(invoice.amount / 2)

    with pytest.raises(ValueError):
        services.record_payment(session, invoice.id, invoice.amount)  # overpayment


def test_supermarket_order_requires_po_number(session, seeded):
    supermarket, product = seeded["supermarket"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)

    with pytest.raises(services.ChannelRequirementError):
        services.place_order(session, supermarket.id, [services.OrderLineRequest(product.id, 2)])

    # rejected before any order/stock change -- not even a rejected-order row
    assert services.finished_goods_stock(session, product.id) == 4

    order = services.place_order(
        session, supermarket.id, [services.OrderLineRequest(product.id, 2)], po_number="PO-4471"
    )
    assert order.status.value == "confirmed"
    assert order.po_number == "PO-4471"
    assert order.packaging == "palletized, GS1-compliant shelf labeling"


def test_international_order_requires_customs_declaration(session, seeded):
    international, product = seeded["international"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)

    with pytest.raises(services.ChannelRequirementError):
        services.place_order(session, international.id, [services.OrderLineRequest(product.id, 2)])

    order = services.place_order(
        session,
        international.id,
        [services.OrderLineRequest(product.id, 2)],
        customs_declaration="CD-EXP-2026-0091",
    )
    assert order.status.value == "confirmed"
    assert order.customs_declaration == "CD-EXP-2026-0091"


def test_company_retail_order_is_internal_transfer_not_invoiced(session, seeded):
    company_retail, product = seeded["company_retail"], seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)

    order = services.place_order(
        session, company_retail.id, [services.OrderLineRequest(product.id, 2)]
    )

    assert order.status.value == "confirmed"
    # stock still moves...
    assert services.finished_goods_stock(session, product.id) == 2
    # ...but no invoice/AR is raised, since this isn't a real sale
    assert services.outstanding_ar(session, company_retail.id) == 0.0
    from app.models import Invoice
    assert session.query(Invoice).filter_by(sales_order_id=order.id).count() == 0


def test_sales_by_channel_reports_across_all_channels(session, seeded):
    product = seeded["product"]
    services.run_production(session, product.id, units_to_produce=4, fabric_roll_id=seeded["roll"].id)
    services.place_order(session, seeded["franchisee"].id, [services.OrderLineRequest(product.id, 1)])

    report = {row["channel"]: row for row in services.sales_by_channel(session)}
    assert report["franchisee"]["order_count"] == 1
    assert report["franchisee"]["total_value"] == pytest.approx(product.unit_price)
