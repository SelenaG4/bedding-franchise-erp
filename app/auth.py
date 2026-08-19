"""Role-based auth: two roles, deliberately kept this simple for the system's
actual scale (a small internal ERP, not a public multi-tenant SaaS product).

- ``admin``: internal ERP staff. One shared secret (``ADMIN_API_KEY``), since
  there's no staff-user table in this system yet -- see README "What I'd do
  next". Admin can see and do everything.
- ``account``: a single sales account (a franchisee, a supermarket partner, a
  walk-in customer's own account, etc.). Authenticates with the per-account
  API key generated when the account was created. Can only see its own
  orders and AR -- never another account's, and never system-wide totals.

Auth is enforced at the API boundary (this module + main.py), not inside
app/services.py -- the service layer stays pure business logic with no
knowledge of who's asking, which is also why the original service-layer
tests in tests/test_erp.py didn't need to change for this.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from app.models import Account

# A real deployment must set this. The fallback exists so the app still runs
# for local/demo use without extra setup, but it's intentionally obvious and
# logged loudly so nobody mistakes it for a real secret.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "dev-admin-key-change-me")
if ADMIN_API_KEY == "dev-admin-key-change-me":
    print(
        "WARNING: ADMIN_API_KEY is not set -- using the insecure default. "
        "Set the ADMIN_API_KEY environment variable before deploying this anywhere real."
    )


def generate_api_key() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class Principal:
    role: str  # "admin" | "account"
    account_id: int | None = None  # set when role == "account"


def get_principal(session: Session, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Principal:
    """Resolve the caller's identity from the X-API-Key header. Raises 401 if
    the key is missing or doesn't match anything -- deliberately doesn't
    distinguish "missing" from "wrong" in the response, same reasoning as any
    login form not confirming which part was incorrect."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    if secrets.compare_digest(x_api_key, ADMIN_API_KEY):
        return Principal(role="admin")

    account = session.query(Account).filter_by(api_key=x_api_key).one_or_none()
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return Principal(role="account", account_id=account.id)


def ensure_account_access(principal: Principal, account_id: int) -> None:
    """Admin can access any account's data. An account principal can only
    access its own -- this is the actual "role-based auth" behavior:
    same endpoint, response scoped by who's asking."""
    if principal.role == "admin":
        return
    if principal.account_id != account_id:
        raise HTTPException(
            status_code=403,
            detail="This API key can only access its own account's data",
        )


def ensure_admin(principal: Principal) -> None:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
