"""Tests for app/optimization.py -- the batch fabric-cutting planner.

The key claim under test isn't just "the MILP runs without crashing": it's
that the optimal plan is never worse than the greedy heuristic (true by
construction, since greedy is one specific feasible assignment the MILP is
also free to choose), and that there exists a concrete, realistic scenario
where it's strictly better -- proving the batch view actually buys something,
not just asserting it in a docstring.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, FabricRoll, Product
from app.optimization import PendingRun, compare_plans, greedy_batch_plan, optimal_batch_plan


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


def test_greedy_is_myopic_optimal_beats_it(session):
    """Two rolls, two runs, sized so the greedy heuristic's first pick (the
    smaller of the two feasible rolls, taken for run 1) blocks the pairing
    that would leave zero scrap. The MILP considers both pairings and finds
    the better one -- this is exactly the batch-vs-one-at-a-time gap the
    module exists to close."""
    product_a = Product(
        sku="BED-A", name="A", product_line="Cotton Sateen", unit_price=90.0, fabric_meters_per_unit=3.1
    )
    product_b = Product(
        sku="BED-B", name="B", product_line="Cotton Sateen", unit_price=90.0, fabric_meters_per_unit=3.0
    )
    roll_a = FabricRoll(
        roll_code="ROLL-A", fabric_type="Cotton Sateen", color="Ivory",
        total_length_m=5.0, remaining_length_m=5.0, is_remnant=False,
    )
    roll_b = FabricRoll(
        roll_code="ROLL-B", fabric_type="Cotton Sateen", color="Ivory",
        total_length_m=5.2, remaining_length_m=5.2, is_remnant=False,
    )
    session.add_all([product_a, product_b, roll_a, roll_b])
    session.commit()

    pending = [
        PendingRun(product_a.id, 1, label="run-1 (needs 3.1m)"),
        PendingRun(product_b.id, 1, label="run-2 (needs 3.0m)"),
    ]

    greedy = greedy_batch_plan(session, pending)
    optimal = optimal_batch_plan(session, pending)

    # Greedy: run-1 (3.1m) picks the smaller feasible roll (A, 5.0m) ->
    # leftover 1.9m, below the 2.0m remnant threshold -> scrap.
    # run-2 (3.0m) is left with roll B (5.2m) -> leftover 2.2m -> remnant, no scrap.
    assert greedy.total_scrap_m == pytest.approx(1.9)

    # Optimal: run-1 -> roll B (5.2m) leftover 2.1m (remnant); run-2 -> roll A
    # (5.0m) leftover 2.0m (exactly the remnant threshold -> not scrap).
    # Both leftovers clear the threshold -> zero scrap.
    assert optimal.total_scrap_m == pytest.approx(0.0)
    assert optimal.total_scrap_m < greedy.total_scrap_m

    comparison = compare_plans(session, pending)
    assert comparison.scrap_reduction_m == pytest.approx(1.9)
    assert comparison.scrap_reduction_pct == pytest.approx(100.0)


def test_optimal_never_worse_than_greedy_on_a_larger_random_batch(session):
    """Not a hand-picked edge case this time -- five runs competing for five
    rolls of varied sizes. The MILP result must be <= the greedy result on
    total scrap, always, since greedy is itself one feasible assignment
    within the MILP's search space."""
    product = Product(
        sku="BED-C", name="C", product_line="Bamboo Weave", unit_price=70.0, fabric_meters_per_unit=1.0
    )
    session.add(product)
    roll_lengths = [6.3, 7.9, 5.1, 9.4, 6.8]
    rolls = [
        FabricRoll(
            roll_code=f"ROLL-{i}", fabric_type="Bamboo Weave", color="Slate",
            total_length_m=length, remaining_length_m=length, is_remnant=False,
        )
        for i, length in enumerate(roll_lengths)
    ]
    session.add_all(rolls)
    session.commit()

    run_sizes = [4.4, 5.9, 3.2, 7.6, 4.9]  # units_to_produce, since fabric_meters_per_unit=1.0
    pending = [PendingRun(product.id, int(size), label=f"run-{i}") for i, size in enumerate(run_sizes)]

    comparison = compare_plans(session, pending)
    assert comparison.optimal.total_scrap_m <= comparison.greedy.total_scrap_m + 1e-9
    assert not comparison.optimal.unassignable
    assert not comparison.greedy.unassignable
    assert len(comparison.optimal.assignments) == len(pending)


def test_unassignable_run_reported_not_silently_dropped(session):
    product = Product(
        sku="BED-D", name="D", product_line="Linen Blend", unit_price=80.0, fabric_meters_per_unit=1.0
    )
    small_roll = FabricRoll(
        roll_code="ROLL-SMALL", fabric_type="Linen Blend", color="Blush",
        total_length_m=2.0, remaining_length_m=2.0, is_remnant=False,
    )
    session.add_all([product, small_roll])
    session.commit()

    pending = [PendingRun(product.id, 50, label="too-big-for-any-roll")]

    greedy = greedy_batch_plan(session, pending)
    optimal = optimal_batch_plan(session, pending)

    assert greedy.unassignable == ["too-big-for-any-roll"]
    assert optimal.unassignable == ["too-big-for-any-roll"]
    assert greedy.assignments == []
    assert optimal.assignments == []


def test_is_scrap_flag_matches_remnant_threshold(session):
    """REMNANT_MIN_LENGTH_M = 2.0m: leftover right at the boundary counts as
    a usable remnant (not scrap), matching app.services.run_production's own
    behavior -- this planner's scrap accounting has to agree with what
    actually happens when the plan is executed."""
    from app.models import REMNANT_MIN_LENGTH_M

    product = Product(
        sku="BED-E", name="E", product_line="Cotton Sateen", unit_price=90.0, fabric_meters_per_unit=1.0
    )
    roll = FabricRoll(
        roll_code="ROLL-E", fabric_type="Cotton Sateen", color="Ivory",
        total_length_m=10.0, remaining_length_m=10.0, is_remnant=False,
    )
    session.add_all([product, roll])
    session.commit()

    units = 10.0 - REMNANT_MIN_LENGTH_M  # leftover lands exactly on the threshold
    pending = [PendingRun(product.id, int(units), label="boundary-run")]
    plan = greedy_batch_plan(session, pending)

    assert len(plan.assignments) == 1
    assert plan.assignments[0].is_scrap is False
    assert plan.total_scrap_m == pytest.approx(0.0)
