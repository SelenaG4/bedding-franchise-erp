"""Batch production planning: joint-optimal fabric-roll assignment via integer
programming, benchmarked against the greedy "remnant-first, best-fit"
heuristic already used one-run-at-a-time by
app.services.allocate_fabric_roll().

The problem this solves: allocate_fabric_roll() makes the best *local* choice
for one production run in isolation -- exactly what happens if a planner
submits runs to POST /production-runs one at a time. In practice a planner
usually queues a batch (a day's worth of pending production requests) before
committing to fabric cuts. Assigning that batch to rolls is a matching
problem: N pending runs, M available rolls, one roll per run, minimizing
total scrap -- and a sequence of locally-best picks can leave more scrap on
the table than a plan that considers the whole batch jointly. This module
solves that batch-assignment problem as a 0/1 integer program via
scipy.optimize.milp (HiGHS solver), and separately reproduces the greedy
heuristic's behavior so the two can be compared on the identical input.

Scope, stated honestly: both planners work off a single static snapshot of
the current roll pool (whatever FabricRoll rows exist with
remaining_length_m > 0 right now). Neither models a remnant created by run A
becoming available to run B within the same batch -- that's a harder,
multi-stage cutting-stock problem (see README's "what I'd do next"). Because
both planners share the same static-snapshot assumption, the comparison
between them is fair even though neither captures that further real-world
optimization opportunity.

The objective is "true waste" in this domain's own terms, not raw leftover
meters: app.models.REMNANT_MIN_LENGTH_M is the line this codebase already
draws between a usable remnant (kept as stock, costs nothing) and scrap
(thrown away). A leftover at or above that threshold costs 0 in the
objective; only leftover below it -- genuine scrap -- is counted.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import REMNANT_MIN_LENGTH_M, FabricRoll, Product


@dataclass(frozen=True)
class PendingRun:
    product_id: int
    units_to_produce: int
    label: str = ""


@dataclass
class RollAssignment:
    run_label: str
    product_id: int
    units_to_produce: int
    meters_needed: float
    roll_id: int
    roll_code: str
    roll_remaining_before: float
    leftover_m: float
    is_scrap: bool  # leftover below REMNANT_MIN_LENGTH_M -- true waste, not a usable remnant


@dataclass
class BatchPlanResult:
    method: str  # "greedy" | "optimal_milp"
    assignments: list[RollAssignment]
    unassignable: list[str]  # run labels with no roll big enough anywhere in the pool
    total_scrap_m: float
    total_leftover_m: float


@dataclass
class PlanComparison:
    greedy: BatchPlanResult
    optimal: BatchPlanResult
    scrap_reduction_m: float
    scrap_reduction_pct: float | None  # None when greedy already had zero scrap


def _scrap_component(leftover: float) -> float:
    return leftover if leftover < REMNANT_MIN_LENGTH_M else 0.0


def _prepare(session: Session, pending_runs: list[PendingRun]):
    """Resolve products and snapshot the current roll pool once, so the
    greedy and optimal planners are compared against the identical input."""
    rolls = list(session.scalars(select(FabricRoll).where(FabricRoll.remaining_length_m > 0)))
    runs = []
    for i, pr in enumerate(pending_runs):
        product = session.get(Product, pr.product_id)
        if product is None:
            raise ValueError(f"Unknown product_id {pr.product_id}")
        label = pr.label or f"run-{i + 1} ({product.sku})"
        meters_needed = product.fabric_meters_per_unit * pr.units_to_produce
        runs.append(
            {
                "label": label,
                "product_id": pr.product_id,
                "product": product,
                "units_to_produce": pr.units_to_produce,
                "meters_needed": meters_needed,
            }
        )
    return runs, rolls


def greedy_batch_plan(session: Session, pending_runs: list[PendingRun]) -> BatchPlanResult:
    """Mirrors app.services.allocate_fabric_roll(): processes runs in the
    given order, each time picking the smallest sufficiently-large roll
    (remnants preferred) -- the same outcome as if each run were POSTed to
    /production-runs one at a time, in this order."""
    runs, rolls = _prepare(session, pending_runs)
    used: set[int] = set()

    assignments: list[RollAssignment] = []
    unassignable: list[str] = []
    total_scrap = 0.0
    total_leftover = 0.0

    for run in runs:
        candidates = sorted(
            (
                r
                for r in rolls
                if r.id not in used
                and r.fabric_type == run["product"].product_line
                and r.remaining_length_m >= run["meters_needed"]
            ),
            key=lambda r: (not r.is_remnant, r.remaining_length_m),
        )
        if not candidates:
            unassignable.append(run["label"])
            continue
        roll = candidates[0]
        leftover = roll.remaining_length_m - run["meters_needed"]
        scrap = _scrap_component(leftover)
        assignments.append(
            RollAssignment(
                run_label=run["label"],
                product_id=run["product_id"],
                units_to_produce=run["units_to_produce"],
                meters_needed=run["meters_needed"],
                roll_id=roll.id,
                roll_code=roll.roll_code,
                roll_remaining_before=roll.remaining_length_m,
                leftover_m=leftover,
                is_scrap=leftover < REMNANT_MIN_LENGTH_M,
            )
        )
        used.add(roll.id)
        total_scrap += scrap
        total_leftover += leftover

    return BatchPlanResult("greedy", assignments, unassignable, total_scrap, total_leftover)


def optimal_batch_plan(session: Session, pending_runs: list[PendingRun]) -> BatchPlanResult:
    """Joint assignment via 0/1 integer programming: minimizes total scrap
    across the whole batch at once, rather than one locally-best choice at a
    time. Solved with scipy.optimize.milp (HiGHS)."""
    runs, rolls = _prepare(session, pending_runs)

    # Every feasible (run, roll) pair: right fabric type, big enough.
    pairs: list[tuple[int, int, float, float]] = []  # (run_idx, roll_idx, leftover, scrap)
    feasible_runs: set[int] = set()
    feasible_rolls: set[int] = set()
    for ri, run in enumerate(runs):
        for rj, roll in enumerate(rolls):
            if roll.fabric_type != run["product"].product_line:
                continue
            if roll.remaining_length_m < run["meters_needed"]:
                continue
            leftover = roll.remaining_length_m - run["meters_needed"]
            pairs.append((ri, rj, leftover, _scrap_component(leftover)))
            feasible_runs.add(ri)
            feasible_rolls.add(rj)

    unassignable = [runs[i]["label"] for i in range(len(runs)) if i not in feasible_runs]

    if not pairs:
        return BatchPlanResult("optimal_milp", [], unassignable, 0.0, 0.0)

    n = len(pairs)
    c = np.array([p[3] for p in pairs], dtype=float)  # minimize total scrap

    run_row = {ri: i for i, ri in enumerate(sorted(feasible_runs))}
    roll_row = {rj: i for i, rj in enumerate(sorted(feasible_rolls))}

    # Each feasible run gets exactly one roll; each roll used by at most one run.
    A_run = np.zeros((len(run_row), n))
    A_roll = np.zeros((len(roll_row), n))
    for k, (ri, rj, _, _) in enumerate(pairs):
        A_run[run_row[ri], k] = 1
        A_roll[roll_row[rj], k] = 1

    constraints = [
        LinearConstraint(A_run, lb=1, ub=1),
        LinearConstraint(A_roll, lb=0, ub=1),
    ]
    result = milp(c, constraints=constraints, integrality=np.ones(n), bounds=Bounds(lb=0, ub=1))

    if not result.success:
        raise RuntimeError(f"MILP solve failed: {result.message}")

    assignments: list[RollAssignment] = []
    total_scrap = 0.0
    total_leftover = 0.0
    for k, x in enumerate(result.x):
        if x > 0.5:
            ri, rj, leftover, scrap = pairs[k]
            run, roll = runs[ri], rolls[rj]
            assignments.append(
                RollAssignment(
                    run_label=run["label"],
                    product_id=run["product_id"],
                    units_to_produce=run["units_to_produce"],
                    meters_needed=run["meters_needed"],
                    roll_id=roll.id,
                    roll_code=roll.roll_code,
                    roll_remaining_before=roll.remaining_length_m,
                    leftover_m=leftover,
                    is_scrap=leftover < REMNANT_MIN_LENGTH_M,
                )
            )
            total_scrap += scrap
            total_leftover += leftover

    return BatchPlanResult("optimal_milp", assignments, unassignable, total_scrap, total_leftover)


def compare_plans(session: Session, pending_runs: list[PendingRun]) -> PlanComparison:
    greedy = greedy_batch_plan(session, pending_runs)
    optimal = optimal_batch_plan(session, pending_runs)
    reduction = greedy.total_scrap_m - optimal.total_scrap_m
    pct = (reduction / greedy.total_scrap_m * 100.0) if greedy.total_scrap_m > 1e-9 else None
    return PlanComparison(greedy, optimal, reduction, pct)
