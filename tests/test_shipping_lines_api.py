"""Wiring tests for the shipping-lines router (no DB required).

Asserts the route surface matches the plan, the ``/{shipping_line}`` catch-all is
declared last (so static paths win), format detection is correct, and the RBAC
policy is registered.
"""
from __future__ import annotations

from gateway.routers import shipping_lines as slr
from services.shipping_lines.service import detect_format


def _paths() -> list[str]:
    return [r.path for r in slr.router.routes]


def test_expected_routes_present():
    paths = set(_paths())
    for p in (
        "/api/shipping-lines/summary",
        "/api/shipping-lines/lines",
        "/api/shipping-lines/delivery-orders",
        "/api/shipping-lines/messages",
        "/api/shipping-lines/messages/{file_id}",
        "/api/shipping-lines/events",
        "/api/shipping-lines/container/{container_number}",
        "/api/shipping-lines/bl/{bill_of_lading}",
        "/api/shipping-lines/import",
        "/api/shipping-lines/{shipping_line}",
    ):
        assert p in paths, f"missing route {p}"


def test_catch_all_declared_last():
    paths = _paths()
    catch_all = "/api/shipping-lines/{shipping_line}"
    static = "/api/shipping-lines/summary"
    assert paths.index(catch_all) > paths.index(static), (
        "the /{shipping_line} catch-all must be declared after the static paths")


def test_detect_format():
    base = "/data/4-Shipping Lines"
    assert detect_format(f"{base}/IAL FORMAT/IAL APMT.csv") == ("IAL", "APMT", "CSV")
    assert detect_format(f"{base}/EAL_FORMAT/EAL_NSICT.xls") == ("EAL", "NSICT", "XLS")
    assert detect_format(f"{base}/IAL FORMAT/IAL NSIGT.xlsx") == ("IAL", "NSIGT", "XLSX")
    assert detect_format(f"{base}/EDO/EDO.xlsx") == ("EDO", "OTHER", "CODECO_XML")


def test_rbac_policy_registered():
    from gateway.auth import _POLICY
    prefixes = {p for p, _roles in _POLICY}
    assert "/api/shipping-lines" in prefixes, "shipping-lines RBAC policy missing"
