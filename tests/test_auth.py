"""HTTP-level tests for role-based auth and the individual-account feature.

These use FastAPI's TestClient rather than calling app.services directly,
because auth is enforced at the API boundary (app/main.py + app/auth.py),
not inside the service layer -- see app/auth.py's module docstring. The
existing tests/test_erp.py tests stay as pure service-layer tests and don't
need auth at all, which is itself a sign the boundary is in the right place.
"""
from __future__ import annotations

import os
import tempfile

os.environ["ADMIN_API_KEY"] = "test-admin-key"
# A real temp file, not "sqlite://" in-memory: the app's engine hands out a
# fresh connection per request, and SQLAlchemy's default pooling gives each
# connection to ":memory:" its own separate empty database -- a temp file is
# the simplest way to get one real shared database across requests in these
# HTTP-level tests.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}"

import pytest
from fastapi.testclient import TestClient

from app.main import app, db
from app.db import engine
from app.models import Base


@pytest.fixture()
def client():
    # Full drop/recreate per test, not just create_all -- these HTTP tests
    # share one engine/file across the whole module (see the DATABASE_URL
    # comment above), so without this, accounts and orders from an earlier
    # test would leak into the next one's assertions.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return TestClient(app)


@pytest.fixture()
def two_accounts(client):
    """One franchisee, one individual/walk-in account, created via the admin
    API so each has a real generated api_key -- then a product + fabric roll
    + stock so both accounts can actually place an order."""
    headers = {"X-API-Key": "test-admin-key"}

    franchisee = client.post(
        "/accounts",
        json={"name": "Alpine Home Textiles", "channel": "franchisee", "location": "Zurich", "credit_limit": 25000},
        headers=headers,
    ).json()

    walkin = client.post(
        "/accounts/individual/lookup-or-create",
        params={"contact_email": "m.herzog@example.com", "name": "Walk-in: M. Herzog", "location": "Zurich"},
        headers=headers,
    ).json()

    product = client.post(
        "/products",
        json={
            "sku": "BED-001",
            "name": "Cotton Sateen Duvet Set - Queen",
            "product_line": "Cotton Sateen",
            "unit_price": 87.0,
            "fabric_meters_per_unit": 2.1,
        },
        headers=headers,
    ).json()
    client.post(
        "/fabric-rolls",
        json={"roll_code": "ROLL-COT-01", "fabric_type": "Cotton Sateen", "color": "Ivory", "total_length_m": 50.0},
        headers=headers,
    )
    client.post("/production-runs", json={"product_id": product["id"], "units_to_produce": 10}, headers=headers)

    return {"franchisee": franchisee, "walkin": walkin, "product": product, "admin_headers": headers}


def test_missing_api_key_is_rejected(client):
    resp = client.get("/accounts/1/outstanding-ar")
    assert resp.status_code == 401


def test_invalid_api_key_is_rejected(client):
    resp = client.get("/accounts/1/outstanding-ar", headers={"X-API-Key": "not-a-real-key"})
    assert resp.status_code == 401


def test_account_can_see_its_own_ar_and_orders(client, two_accounts):
    franchisee = two_accounts["franchisee"]
    headers = {"X-API-Key": franchisee["api_key"]}

    ar = client.get(f"/accounts/{franchisee['id']}/outstanding-ar", headers=headers)
    assert ar.status_code == 200
    assert ar.json()["account_id"] == franchisee["id"]

    orders = client.get(f"/accounts/{franchisee['id']}/orders", headers=headers)
    assert orders.status_code == 200
    assert orders.json()["account_id"] == franchisee["id"]


def test_account_cannot_see_another_accounts_ar_or_orders(client, two_accounts):
    franchisee, walkin = two_accounts["franchisee"], two_accounts["walkin"]
    headers = {"X-API-Key": franchisee["api_key"]}

    ar = client.get(f"/accounts/{walkin['id']}/outstanding-ar", headers=headers)
    assert ar.status_code == 403

    orders = client.get(f"/accounts/{walkin['id']}/orders", headers=headers)
    assert orders.status_code == 403


def test_account_cannot_place_order_on_behalf_of_another_account(client, two_accounts):
    franchisee, walkin, product = two_accounts["franchisee"], two_accounts["walkin"], two_accounts["product"]
    headers = {"X-API-Key": franchisee["api_key"]}

    resp = client.post(
        "/sales-orders",
        json={"account_id": walkin["id"], "lines": [{"product_id": product["id"], "quantity": 1}]},
        headers=headers,
    )
    assert resp.status_code == 403


def test_account_can_place_and_then_see_its_own_order(client, two_accounts):
    walkin, product = two_accounts["walkin"], two_accounts["product"]
    headers = {"X-API-Key": walkin["api_key"]}

    placed = client.post(
        "/sales-orders",
        json={"account_id": walkin["id"], "lines": [{"product_id": product["id"], "quantity": 1}]},
        headers=headers,
    )
    assert placed.status_code == 200

    orders = client.get(f"/accounts/{walkin['id']}/orders", headers=headers).json()["orders"]
    assert len(orders) == 1
    assert orders[0]["id"] == placed.json()["id"]


def test_admin_can_see_any_accounts_ar_and_system_reports(client, two_accounts):
    walkin, admin_headers = two_accounts["walkin"], two_accounts["admin_headers"]

    ar = client.get(f"/accounts/{walkin['id']}/outstanding-ar", headers=admin_headers)
    assert ar.status_code == 200

    summary = client.get("/reports/summary", headers=admin_headers)
    assert summary.status_code == 200

    by_channel = client.get("/reports/sales-by-channel", headers=admin_headers)
    assert by_channel.status_code == 200


def test_account_cannot_see_system_wide_reports(client, two_accounts):
    headers = {"X-API-Key": two_accounts["franchisee"]["api_key"]}

    assert client.get("/reports/summary", headers=headers).status_code == 403
    assert client.get("/reports/sales-by-channel", headers=headers).status_code == 403


def test_individual_accounts_get_separate_ar_not_a_shared_bucket(client, two_accounts):
    """The actual point of the walk-in-as-own-account feature: two different
    individual customers must never share one account's AR/order history."""
    walkin, product, headers = two_accounts["walkin"], two_accounts["product"], two_accounts["admin_headers"]

    second_walkin = client.post(
        "/accounts/individual/lookup-or-create",
        params={"contact_email": "a.keller@example.com", "name": "Walk-in: A. Keller", "location": "Zurich"},
        headers=headers,
    ).json()
    assert second_walkin["id"] != walkin["id"]

    client.post(
        "/sales-orders",
        json={"account_id": walkin["id"], "lines": [{"product_id": product["id"], "quantity": 2}]},
        headers={"X-API-Key": walkin["api_key"]},
    )

    walkin_orders = client.get(f"/accounts/{walkin['id']}/orders", headers=headers).json()["orders"]
    second_orders = client.get(f"/accounts/{second_walkin['id']}/orders", headers=headers).json()["orders"]
    assert len(walkin_orders) == 1
    assert len(second_orders) == 0  # never sees the first customer's order


def test_lookup_or_create_individual_account_is_idempotent_per_email(client, two_accounts):
    headers = two_accounts["admin_headers"]
    again = client.post(
        "/accounts/individual/lookup-or-create",
        params={"contact_email": "m.herzog@example.com", "name": "Walk-in: M. Herzog", "location": "Zurich"},
        headers=headers,
    ).json()
    assert again["id"] == two_accounts["walkin"]["id"]  # same customer, same account -- not a duplicate
