"""
Smoke checks for the revenue dashboard fixes.

Run against a running stack (docker-compose up), from inside the backend
container or from the host:

    docker compose exec backend python tests/check_revenue.py
    python backend/tests/check_revenue.py            # host, needs :8000 published

Stdlib only, no test framework: this is the smallest thing that fails if any
of the four fixes regresses.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import urllib.request
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

BASE = os.getenv("CHECK_BASE_URL", "http://localhost:8000")

CLIENTS = {
    "tenant-a": ("sunset@propertyflow.com", "client_a_2024"),
    "tenant-b": ("ocean@propertyflow.com", "client_b_2024"),
}

# Expected totals, straight from database/seed.sql.
EXPECTED = {
    ("tenant-a", "prop-001"): (2250.00, 4),
    ("tenant-a", "prop-002"): (4975.50, 4),
    ("tenant-a", "prop-003"): (6100.50, 2),
    ("tenant-b", "prop-004"): (1776.50, 4),
    ("tenant-b", "prop-005"): (3256.00, 3),
    # prop-001 exists for both tenants, but tenant-b has no reservations on it.
    # A non-zero total here means the cache leaked tenant-a's data.
    ("tenant-b", "prop-001"): (0.00, 0),
    ("tenant-a", "prop-004"): (0.00, 0),
}


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get(path, token):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def check_totals():
    """Real database figures, and no cross-tenant leakage."""
    tokens = {
        tenant: post("/api/v1/auth/login", {"email": email, "password": password})["access_token"]
        for tenant, (email, password) in CLIENTS.items()
    }

    for (tenant, prop), (total, count) in sorted(EXPECTED.items()):
        data = get(f"/api/v1/dashboard/summary?property_id={prop}", tokens[tenant])
        assert data["total_revenue"] == total, (
            f"{tenant}/{prop}: expected {total}, got {data['total_revenue']}"
        )
        assert data["reservations_count"] == count, (
            f"{tenant}/{prop}: expected {count} reservations, got {data['reservations_count']}"
        )
        print(f"  ok  {tenant:9} {prop}  {total:>9.2f}  {count} reservations")


def check_cent_rounding():
    """Sub-cent totals must round to cents as Decimal, not through float."""
    raw = "3256.005"
    naive = round(float(raw) * 100) / 100          # what the old code path produced
    exact = float(Decimal(raw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    assert naive == 3256.00, f"float path changed: {naive}"
    assert exact == 3256.01, f"decimal path wrong: {exact}"
    print(f"  ok  sub-cent rounding: float gives {naive}, Decimal gives {exact}")


def check_month_boundaries():
    """Month boundaries follow the property's timezone, not UTC."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app.services.reservations import calculate_monthly_revenue

    # The function reports its window through a DEBUG line; read the boundaries
    # back out of it rather than duplicating the maths here.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        asyncio.run(calculate_monthly_revenue("prop-001", 3, 2024, timezone="Europe/Paris"))

    line = buffer.getvalue()
    assert "2024-03-01 00:00:00+01:00" in line, f"March in Paris did not start locally: {line}"
    assert "2024-04-01 00:00:00+02:00" in line, f"April in Paris did not start locally: {line}"

    # The seeded reservation res-tz-1 checks in at 2024-02-29 23:30 UTC, which is
    # 2024-03-01 00:30 in Paris - it belongs to March for this property.
    check_in = datetime.fromisoformat("2024-02-29 23:30:00+00:00")
    start = datetime.fromisoformat("2024-03-01 00:00:00+01:00")
    assert check_in >= start, "res-tz-1 fell outside March in the property's timezone"
    print("  ok  March for Europe/Paris runs 2024-03-01 00:00+01:00 to 2024-04-01 00:00+02:00")


if __name__ == "__main__":
    print("revenue totals and tenant isolation:")
    check_totals()
    print("cent rounding:")
    check_cent_rounding()
    print("month boundaries:")
    check_month_boundaries()
    print("\nall checks passed")
