"""Tests for the FASTag demo dataset and its seeding path (no DB, no network).

Three layers:

* **Dataset invariants** — the demo spec, asserted directly: 10 vehicles, ACTIVE
  accounts, ₹500-₹5000 balances, 5-10 crossings inside a 30-day window, SUCCESS
  status, completed trips with origin/destination/distance/tolls-crossed.
* **Determinism** — the same plate must produce byte-identical data on every
  call *and in a fresh interpreter*. The subprocess check is the one that
  matters: an earlier draft keyed off :func:`hash`, which is salted per process,
  so the seeded RDS rows and a later live demo fetch would have silently
  disagreed on every tag id and ``seq_no``.
* **Mapper round-trip** — the regression guard for the bug that caused the empty
  cards. ``demo_transactions`` used to return a bare list under ``data``, which
  ``FastagTransactionBatch`` could only read as an unmapped key, so the mapper
  produced ZERO rows and the Transactions / Journey / History views were empty
  no matter how many times the demo was refreshed.

The real DB write path is exercised by ``scripts/seed_fastag_demo.py --verify``;
nothing here needs Postgres.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.fastag import demo_dataset as ds  # noqa: E402
from services.fastag.demo_provider import (  # noqa: E402
    demo_balance,
    demo_toll_enroute,
    demo_transactions,
)
from services.fastag.mappers import (  # noqa: E402
    map_fastag_balance,
    map_fastag_transactions,
    map_toll_enroute,
)

ALL_PLATES = ds.SEED_PLATES

#: Plates deliberately outside the demo fleet — an operator can search anything.
UNKNOWN_PLATES = ("MH01ZZ9999", "DL03CA0007", "WB19XY4242")


@pytest.fixture(scope="module")
def now() -> datetime:
    """A fixed reference instant, so age assertions can't race the clock."""
    return datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ dataset invariants
def test_all_ten_demo_vehicles_are_present():
    assert len(ALL_PLATES) == 10
    assert set(ALL_PLATES) == set(ds.FLEET)
    assert len(set(ALL_PLATES)) == 10, "duplicate plate in the seed list"


@pytest.mark.parametrize("rc", ALL_PLATES)
def test_account_is_active_with_balance_in_band(rc):
    account = ds.account_payload(rc)
    assert account["rc_number"] == rc
    assert account["tag_status"] == "ACTIVE"
    assert account["tag_id"] and len(account["tag_id"]) == 24
    assert account["provider_name"]
    assert account["customer_name"]

    balance = Decimal(account["available_balance"])
    assert ds.BALANCE_MIN <= balance <= ds.BALANCE_MAX, f"{rc} balance {balance} out of band"
    # Wallet headroom must stay consistent with the balance, not be a free number.
    assert Decimal(account["available_recharge_limit"]) == Decimal("10000.00") - balance


def test_vehicles_are_distinguishable_from_each_other():
    """Every vehicle needs its own identity — the old demo gave all of them the
    same tag id, bank and balance, which made the screen look broken."""
    for field in ("tag_id", "provider_name", "customer_name"):
        values = [ds.account_payload(rc)[field] for rc in ALL_PLATES]
        assert len(set(values)) == len(values), f"{field} repeats across vehicles"
    balances = {ds.account_payload(rc)["available_balance"] for rc in ALL_PLATES}
    assert len(balances) == len(ALL_PLATES)


@pytest.mark.parametrize("rc", ALL_PLATES)
def test_transactions_meet_the_spec(rc, now):
    rows = ds.transactions_payload(rc, now=now)["transactions"]
    assert ds.TXN_MIN <= len(rows) <= ds.TXN_MAX, f"{rc} has {len(rows)} crossings"

    window_start = now - timedelta(days=ds.WINDOW_DAYS)
    stamps = []
    for row in rows:
        assert row["status"] == "SUCCESS"
        assert row["toll_plaza_name"] in {p.name for p in ds.PLAZAS.values()}
        assert Decimal(row["amount"]) > 0
        assert row["rc_number"] == rc
        ts = datetime.fromisoformat(row["transaction_date_time"])
        assert window_start <= ts <= now, f"{rc} crossing {ts} outside the 30-day window"
        stamps.append(ts)

    assert stamps == sorted(stamps, reverse=True), "batch must be newest-first"
    assert len({r["seq_no"] for r in rows}) == len(rows), "seq_no repeats within a batch"


def test_seq_numbers_are_globally_unique_across_the_fleet(now):
    """seq_no is the UNIQUE idempotency key on core.fastag_transaction — a clash
    between two vehicles would silently drop one vehicle's crossing."""
    seen: dict[str, str] = {}
    for rc in ALL_PLATES + UNKNOWN_PLATES:
        for row in ds.transactions_payload(rc, now=now)["transactions"]:
            assert row["seq_no"] not in seen, (
                f"{rc} reuses seq_no {row['seq_no']} already held by {seen.get(row['seq_no'])}"
            )
            seen[row["seq_no"]] = rc


@pytest.mark.parametrize("rc", ALL_PLATES)
def test_trips_are_complete_and_carry_journey_detail(rc, now):
    trips = ds.trips_for(rc, now=now)
    assert trips, f"{rc} has no trips"
    assert all(t.completed for t in trips), f"{rc} has an unfinished leg"

    journeys = ds.journeys_payload(rc, now=now)
    assert len(journeys) == len(trips)
    for journey in journeys:
        assert journey["source_name"] and journey["destination_name"]
        assert journey["source_name"] != journey["destination_name"]
        assert Decimal(journey["distance"]) > 0
        assert journey["duration"]
        assert journey["_tolls_crossed"] == len(journey["toll_plaza_details"]) > 0
        assert journey["_completed"] is True
        assert Decimal(journey["_total_toll"]) > 0
        assert journey["client_id"] == f"demo-seed:{rc}"
        for plaza in journey["toll_plaza_details"]:
            assert Decimal(plaza["cost"]) > 0
            assert -90 <= plaza["toll_plaza_latitude"] <= 90
            assert -180 <= plaza["toll_plaza_longitude"] <= 180


def test_trips_alternate_direction_so_the_journey_reads_as_round_trips(now):
    """A vehicle that only ever drives away from the port is not a fleet."""
    multi = [rc for rc in ALL_PLATES if len(ds.trips_for(rc, now=now)) > 1]
    assert multi, "expected at least one vehicle with more than one leg"
    for rc in multi:
        legs = [t.outbound for t in ds.trips_for(rc, now=now)]
        assert all(a != b for a, b in zip(legs, legs[1:])), f"{rc} legs do not alternate"


@pytest.mark.parametrize("rc", ALL_PLATES)
def test_health_reports_an_active_recently_seen_tag(rc, now):
    health = ds.health_payload(rc, now=now)
    assert health["active"] is True
    assert health["tag_status"] == "ACTIVE"
    assert health["last_seen"] is not None
    assert health["signal_status"] in {"STRONG", "GOOD", "WEAK"}
    assert 0 <= health["last_seen_age_hours"] <= ds.MAX_LAST_SEEN_HOURS + 1
    assert health["reads_30d"] >= ds.TXN_MIN


# ------------------------------------------------------------------- determinism
@pytest.mark.parametrize("rc", ALL_PLATES[:3] + UNKNOWN_PLATES[:1])
def test_repeated_calls_return_identical_data(rc, now):
    assert ds.account_payload(rc) == ds.account_payload(rc)
    assert ds.transactions_payload(rc, now=now) == ds.transactions_payload(rc, now=now)
    assert ds.journeys_payload(rc, now=now) == ds.journeys_payload(rc, now=now)


def test_data_is_stable_across_interpreters():
    """Fresh processes with different hash seeds must agree exactly.

    This is what keeps the seeded RDS history and a later live demo fetch in
    sync: same tag ids, same seq_no, so ``ON CONFLICT (seq_no) DO NOTHING``
    recognises a replay instead of piling up near-duplicate crossings.
    """
    prog = (
        "import json,sys;"
        f"sys.path[:0]=[{str(REPO_ROOT / 'shared')!r},{str(REPO_ROOT)!r}];"
        "from datetime import datetime,timezone;"
        "from services.fastag import demo_dataset as ds;"
        "ref=datetime(2026,8,6,12,0,tzinfo=timezone.utc);"
        "print(json.dumps([[ds.account_payload(rc),ds.transactions_payload(rc,now=ref)]"
        " for rc in ds.SEED_PLATES],sort_keys=True))"
    )

    def run(seed: str) -> str:
        out = subprocess.run(
            [sys.executable, "-c", prog], capture_output=True, text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"}, timeout=120,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip().splitlines()[-1]

    first, second = run("0"), run("12345")
    assert json.loads(first) == json.loads(second)


def test_seq_no_does_not_move_with_the_clock(now):
    """The stored history must stay pinned at 5-10 crossings however often the
    screen is refreshed.

    Crossing *times* are relative to now, so if ``seq_no`` were derived from a
    timestamp it would mint fresh keys every time the window rolled over a day
    boundary — each refresh appending another near-duplicate batch instead of
    being skipped by ``ON CONFLICT (seq_no) DO NOTHING``.
    """
    for rc in ALL_PLATES:
        keys_now = [r["seq_no"] for r in ds.transactions_payload(rc, now=now)["transactions"]]
        for offset in (timedelta(hours=7), timedelta(days=3), timedelta(days=45)):
            later = ds.transactions_payload(rc, now=now + offset)["transactions"]
            assert [r["seq_no"] for r in later] == keys_now, f"{rc} seq_no drifted at +{offset}"
            # …while the crossings themselves stay current relative to the new now.
            newest = max(datetime.fromisoformat(r["transaction_date_time"]) for r in later)
            assert (now + offset - newest) <= timedelta(hours=ds.MAX_LAST_SEEN_HOURS + 1)


def test_unknown_plate_still_produces_a_full_account(now):
    """An operator searching a plate outside the demo fleet must not hit an empty
    card — the fallback profile is synthesised from the plate, deterministically."""
    for rc in UNKNOWN_PLATES:
        account = ds.account_payload(rc)
        assert account["tag_status"] == "ACTIVE"
        assert ds.BALANCE_MIN <= Decimal(account["available_balance"]) <= ds.BALANCE_MAX
        assert ds.TXN_MIN <= len(ds.transactions_payload(rc, now=now)["transactions"]) <= ds.TXN_MAX
        assert ds.journeys_payload(rc, now=now)
        assert ds.profile_for(rc).rc_number == rc


def test_lowercase_and_spaced_plates_resolve_to_the_same_vehicle():
    assert ds.account_payload("mh04ab1234") == ds.account_payload("MH04AB1234")
    assert ds.account_payload("MH 04 AB 1234") == ds.account_payload("MH04AB1234")


# -------------------------------------------------------------- mapper round-trip
@pytest.mark.parametrize("rc", ALL_PLATES)
def test_balance_payload_maps_to_a_db_row(rc):
    mapped = map_fastag_balance(demo_balance(rc), client_id="test")
    assert mapped["status"] == "success", mapped.get("reason")
    db = mapped["db"]
    assert db["rc_number"] == rc
    assert db["tag_status"] == "Activated"        # ACTIVE is normalised by the mapper
    assert isinstance(db["available_balance"], Decimal)
    assert ds.BALANCE_MIN <= db["available_balance"] <= ds.BALANCE_MAX
    assert db["tag_id"] and db["provider_name"] and db["provider_code"]


@pytest.mark.parametrize("rc", ALL_PLATES)
def test_transactions_payload_maps_to_db_rows(rc):
    """REGRESSION: the previous demo shape mapped to zero rows — the direct cause
    of the empty Transactions / Journey / History cards."""
    mapped = map_fastag_transactions(demo_transactions(rc), client_id="test")
    assert mapped["status"] == "success", mapped.get("reason")
    rows = mapped["db"]
    assert ds.TXN_MIN <= len(rows) <= ds.TXN_MAX, f"{rc} mapped to {len(rows)} rows"
    for row in rows:
        assert row["rc_number"] == rc
        assert row["status"] == "SUCCESS"
        assert row["seq_no"] and isinstance(row["seq_no"], str)
        assert row["tag_id"] and row["bank_name"] and row["toll_plaza_name"]
        assert row["vehicle_type"].startswith("VC")
        assert row["transaction_date_time"].tzinfo is not None
        # The mapper splits the raw "lat,lng" it persists.
        assert len(row["toll_plaza_geocode"].split(",")) == 2


def test_transaction_amount_is_reported_as_an_unmapped_vendor_field():
    """``core.fastag_transaction`` has no amount column, so a crossing's fare
    cannot persist per row. It is emitted on the vendor payload and surfaces via
    ``unmapped_fields`` — the designed signal — and is persisted per plaza on
    ``core.toll_enroute``. Asserted so the gap stays visible rather than silent.
    """
    mapped = map_fastag_transactions(demo_transactions(ALL_PLATES[0]), client_id="test")
    assert "amount" in mapped["unmapped_fields"]
    assert all("amount" not in row for row in mapped["db"])

    journey = ds.journeys_payload(ALL_PLATES[0])[0]
    assert all(Decimal(p["cost"]) > 0 for p in journey["toll_plaza_details"])


def test_toll_enroute_payload_maps_and_matches_the_route():
    payload = {
        "clientId": "test", "sourceState": "Maharashtra", "sourceName": "Nhava Sheva",
        "destinationState": "Maharashtra", "destinationName": "Pune", "vehicleType": "TRUCK",
    }
    mapped = map_toll_enroute(demo_toll_enroute(payload), client_id="test")
    assert mapped["status"] == "success", mapped.get("reason")
    db = mapped["db"]
    assert db["source_name"] == "Nhava Sheva"
    assert db["destination_name"] == "Pune"
    assert db["distance"] == Decimal("148.50")     # unit suffix stripped by the DTO
    plazas = db["toll_plaza_details"]
    assert len(plazas) == len(ds.CORRIDORS["pune"].plaza_keys)
    assert plazas[0]["name"] == "JNPA Nhava Sheva Toll Plaza"
    assert all(Decimal(p["cost"]) > 0 and p["lat"] and p["lng"] for p in plazas)


def test_toll_enroute_reverses_plazas_on_the_return_leg():
    out = ds.enroute_payload("Maharashtra", "Nhava Sheva", "Maharashtra", "Pune", "TRUCK")
    back = ds.enroute_payload("Maharashtra", "Pune", "Maharashtra", "Nhava Sheva", "TRUCK")
    names = [p["toll_plaza_name"] for p in out["toll_plaza_details"]]
    assert [p["toll_plaza_name"] for p in back["toll_plaza_details"]] == list(reversed(names))


def test_toll_enroute_falls_back_rather_than_returning_nothing():
    """An unrecognised route must still answer with real plazas — an empty plaza
    table reads as a broken screen."""
    result = ds.enroute_payload("Assam", "Guwahati", "Odisha", "Cuttack", "TRUCK")
    assert result["toll_plaza_details"]


def test_heavier_vehicles_are_charged_more_at_the_same_plaza():
    truck = ds.enroute_payload("Maharashtra", "Nhava Sheva", "Maharashtra", "Pune", "TRUCK")
    mav = ds.enroute_payload("Maharashtra", "Nhava Sheva", "Maharashtra", "Pune", "MAV")
    for a, b in zip(truck["toll_plaza_details"], mav["toll_plaza_details"]):
        assert a["toll_plaza_name"] == b["toll_plaza_name"]
        assert Decimal(b["cost"]) > Decimal(a["cost"])


def test_every_corridor_references_a_known_plaza():
    for corridor in ds.CORRIDORS.values():
        assert corridor.plaza_keys, f"{corridor.key} has no plazas"
        for key in corridor.plaza_keys:
            assert key in ds.PLAZAS, f"{corridor.key} references unknown plaza {key}"
