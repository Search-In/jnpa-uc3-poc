"""Deterministic Vahan/Sarathi/FASTag dataset generator for the JNPA UC-III PoC.

Generates a *fully deterministic* corpus of 25,000 Indian vehicle records across
the MH-04, MH-43, MH-06, GJ-01, KA-01, TN-22, KL-07 RTO series (plus a slice of
new BH-series plates). The same `SEED` always produces the same plates, owners,
validity dates and FASTag balances, so the demo (Prompt 9) and the tests are
reproducible run-to-run and host-to-host.

Realistic anomaly distributions (per spec):
    *  8 %  expired fitness
    *  3 %  blacklisted (RC)
    *  5 %  FASTag LOW_BALANCE
    *  1 %  FASTag BLACKLISTED

This module is importable (the FastAPI app builds its in-memory store from
`generate_dataset()`), and runnable as a script to (re)write the deterministic
test fixture `./data/fixtures/known_plates.json`:

    python -m vahan_sim.seed --out data/fixtures/known_plates.json

All dates are anchored to a fixed `REFERENCE_DATE` (not "today") so expiry is
deterministic regardless of when the seed runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from jnpa_shared.schemas import (
    BlacklistStatus,
    FastagStatus,
    SarathiRecord,
    VahanRecord,
    mask_owner_name,
)

# --- Determinism anchors -----------------------------------------------------
SEED = "jnpa-uc3-vahan-sim-v1"
REFERENCE_DATE = date(2026, 6, 13)  # anchor for "valid_to" expiry math
TOTAL_PLATES = 25_000

# RTO series -> (state, rto_code). The new BH series is handled separately.
SERIES = [
    ("MH04", "Maharashtra", "MH04"),   # Mumbai (Andheri)
    ("MH43", "Maharashtra", "MH43"),   # Navi Mumbai (Vashi) — JNPA's own RTO
    ("MH06", "Maharashtra", "MH06"),   # Alibag / Raigad
    ("GJ01", "Gujarat", "GJ01"),       # Ahmedabad
    ("KA01", "Karnataka", "KA01"),     # Bengaluru (Koramangala)
    ("TN22", "Tamil Nadu", "TN22"),    # Chennai (Meenambakkam)
    ("KL07", "Kerala", "KL07"),        # Ernakulam
]

VEHICLE_CLASSES = ["HGV", "HGV", "HGV", "LGV", "MGV", "CAR", "BUS"]  # truck-heavy
FUEL_TYPES = ["DIESEL", "DIESEL", "DIESEL", "CNG", "PETROL", "ELECTRIC"]
BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "PAYTM", "IDFC"]
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"  # drop I/O/Q to avoid digit confusion

# A small bank of plausible Indian names; masked before they ever leave the box.
FIRST_NAMES = [
    "RAJESH", "SUNIL", "AMIT", "PRADEEP", "SANTOSH", "VIKAS", "RAMESH",
    "MAHESH", "GANESH", "DINESH", "PRAKASH", "ANIL", "SURESH", "MANOJ",
    "DEEPAK", "ASHOK", "VIJAY", "RAVI", "ARJUN", "KIRAN",
]
LAST_NAMES = [
    "PATIL", "SHARMA", "KUMAR", "SHINDE", "YADAV", "NAIR", "REDDY",
    "GOWDA", "DESAI", "JADHAV", "SINGH", "VERMA", "PILLAI", "RAO",
    "KULKARNI", "MORE", "GUPTA", "MENON", "IYER", "JOSHI",
]


def _h(*parts: object) -> int:
    """Stable 64-bit-ish int hash from SEED + parts (not Python's salted hash)."""
    raw = SEED + "|" + "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big")


def _pick(seq, *parts) -> object:
    return seq[_h(*parts) % len(seq)]


def _pct(value: int, *parts) -> bool:
    """True for roughly `value`% of inputs, deterministically."""
    return (_h("pct", *parts) % 10_000) < value * 100


@dataclass(frozen=True)
class SeedRecord:
    """One generated vehicle: its RC, the owner's DL, and a FASTag reading."""

    rc: VahanRecord
    dl: SarathiRecord
    fastag_status: FastagStatus
    fastag_balance: float
    fastag_bank: str
    fastag_tag_id: str


def _plate_for_index(i: int) -> tuple[str, str, str]:
    """Return (plate, state, rto_code). ~2% of plates are new BH-series."""
    if _h("isbh", i) % 100 < 2:
        # BH series: YY BH NNNN LL  e.g. 22BH1234AA
        yy = 21 + (_h("bhyy", i) % 5)            # 21..25
        num = _h("bhnum", i) % 10_000
        l1 = LETTERS[_h("bhl1", i) % len(LETTERS)]
        l2 = LETTERS[_h("bhl2", i) % len(LETTERS)]
        plate = f"{yy:02d}BH{num:04d}{l1}{l2}"
        # BH plates are nationally portable; attribute the state by index anyway.
        _, state, rto = SERIES[_h("bhstate", i) % len(SERIES)]
        return plate, state, "BH"

    prefix, state, rto = SERIES[i % len(SERIES)]
    l1 = LETTERS[_h("l1", i) % len(LETTERS)]
    l2 = LETTERS[_h("l2", i) % len(LETTERS)]
    num = (_h("num", i) % 9999) + 1             # 0001..9999
    plate = f"{prefix}{l1}{l2}{num:04d}"
    return plate, state, rto


def _owner_name(i: int) -> str:
    return f"{_pick(FIRST_NAMES, 'fn', i)} {_pick(LAST_NAMES, 'ln', i)}"


def _dl_number(i: int, rto_code: str) -> str:
    # SS RR NNNNNNNNNNN  — 2-letter state, 2-digit RTO, 11 digits.
    ss = rto_code[:2] if rto_code != "BH" else _pick([s[0][:2] for s in SERIES], "dlss", i)
    rr = _h("dlrr", i) % 100
    serial = _h("dlserial", i) % 100_000_000_000
    return f"{ss}{rr:02d}{serial:011d}"


def _build_record(i: int) -> SeedRecord:
    plate, state, rto = _plate_for_index(i)
    name = _owner_name(i)
    masked = mask_owner_name(name)

    # Registration 1-12 years before the reference date.
    reg_days = 365 + _h("reg", i) % (365 * 11)
    registration_date = REFERENCE_DATE - timedelta(days=reg_days)

    # --- Anomaly draws -------------------------------------------------------
    expired_fitness = _pct(8, "fitness", i)
    blacklisted = _pct(3, "blacklist", i)
    fastag_low = _pct(5, "fastaglow", i)
    fastag_black = _pct(1, "fastagblack", i)

    # Fitness valid_to: expired => 5..400 days in the past; else 30..900 ahead.
    if expired_fitness:
        fitness_valid_to = REFERENCE_DATE - timedelta(days=5 + _h("fdpast", i) % 395)
    else:
        fitness_valid_to = REFERENCE_DATE + timedelta(days=30 + _h("fdfut", i) % 870)

    # PUC + insurance: mostly valid, independently dated for realism.
    puc_valid_to = REFERENCE_DATE + timedelta(days=10 + _h("puc", i) % 350)
    insurance_valid_to = REFERENCE_DATE + timedelta(days=15 + _h("ins", i) % 700)

    rc = VahanRecord(
        rc_number=plate,
        plate=plate,
        owner_name_masked=masked,
        vehicle_class=_pick(VEHICLE_CLASSES, "vc", i),
        fuel_type=_pick(FUEL_TYPES, "ft", i),
        fitness_valid_to=fitness_valid_to,
        puc_valid_to=puc_valid_to,
        insurance_valid_to=insurance_valid_to,
        registration_date=registration_date,
        state=state,
        rto_code=rto,
        blacklist_status=BlacklistStatus.BLACKLISTED if blacklisted else BlacklistStatus.CLEAR,
    )

    dl = SarathiRecord(
        dl_number=_dl_number(i, rto),
        holder_name_masked=masked,
        date_of_issue=registration_date - timedelta(days=_h("dli", i) % 2000),
        valid_to=REFERENCE_DATE + timedelta(days=200 + _h("dlv", i) % 3000),
        vehicle_classes=["LMV", "HMV"] if rc.vehicle_class in {"HGV", "MGV", "BUS"} else ["LMV"],
        state=state,
        rto_code=rto,
        blacklist_status=BlacklistStatus.BLACKLISTED if blacklisted else BlacklistStatus.CLEAR,
    )

    # FASTag: BLACKLISTED dominates LOW_BALANCE if both draw.
    if fastag_black:
        fstatus = FastagStatus.BLACKLISTED
        balance = round(50 + _h("fbal_b", i) % 200, 2)
    elif fastag_low:
        fstatus = FastagStatus.LOW_BALANCE
        balance = round(5 + _h("fbal_l", i) % 95, 2)        # < 100 INR
    else:
        fstatus = FastagStatus.ACTIVE
        balance = round(150 + _h("fbal_a", i) % 4850, 2)    # 150..5000 INR

    tag_id = f"34161FA{_h('tag', i) % 10**12:012d}"

    return SeedRecord(
        rc=rc,
        dl=dl,
        fastag_status=fstatus,
        fastag_balance=balance,
        fastag_bank=_pick(BANKS, "bank", i),
        fastag_tag_id=tag_id,
    )


# Well-known plates that must always resolve so the documented verification
# commands (README / spec) and the init.sql vehicle_master seed work as written.
# Each is built with the same deterministic logic, just on a forced plate.
PINNED_PLATES = ["MH04AB1234", "MH43CD5678"]


def _build_record_for_plate(plate: str, i: int) -> SeedRecord:
    """Build a record with all deterministic attributes of index `i` but on a
    forced (well-known) plate. State/RTO are inferred from the plate prefix."""
    base = _build_record(i)
    prefix = plate[:4]
    state, rto = next(((s, r) for (p, s, r) in SERIES if p == prefix),
                      (base.rc.state, base.rc.rto_code))
    rc = base.rc.model_copy(update={"rc_number": plate, "plate": plate,
                                    "state": state, "rto_code": rto})
    dl = base.dl.model_copy(update={"state": state, "rto_code": rto})
    return SeedRecord(
        rc=rc, dl=dl,
        fastag_status=base.fastag_status,
        fastag_balance=base.fastag_balance,
        fastag_bank=base.fastag_bank,
        fastag_tag_id=base.fastag_tag_id,
    )


def generate_dataset(total: int = TOTAL_PLATES) -> Dict[str, SeedRecord]:
    """Build the full deterministic dataset keyed by normalized plate.

    Collisions (two indices producing the same plate) are rare but possible;
    later indices win, so the count may be marginally below `total`. We
    backfill to guarantee at least `total` distinct plates. A handful of
    well-known plates (PINNED_PLATES) are always injected so the documented
    verification commands resolve.
    """
    out: Dict[str, SeedRecord] = {}
    i = 0
    # Generate until we have `total` distinct plates (backfilling collisions).
    while len(out) < total:
        rec = _build_record(i)
        out[rec.rc.rc_number] = rec
        i += 1
        if i > total * 2:  # safety valve; should never trigger
            break

    # Inject the pinned plates (deterministic; index keyed off the name).
    for plate in PINNED_PLATES:
        idx = total + (_h("pinned", plate) % total)
        out[plate] = _build_record_for_plate(plate, idx)
    return out


def build_dl_index(dataset: Dict[str, SeedRecord]) -> Dict[str, SarathiRecord]:
    """Index Sarathi records by DL number for /sarathi/dl/{dl_number} lookups."""
    return {rec.dl.dl_number: rec.dl for rec in dataset.values()}


# ---------------------------------------------------------------------------
# Fixture writer — the 50 plates the demo (Prompt 9) will query.
# ---------------------------------------------------------------------------
def select_known_plates(dataset: Dict[str, SeedRecord], n: int = 50) -> List[dict]:
    """Pick `n` plates: half guaranteed-benign, half carrying >=1 issue.

    Returns a list of small dicts describing each plate and *why* it was
    chosen, so the demo/tests can assert against concrete expectations.
    """
    benign: List[dict] = []
    issue: List[dict] = []
    half = n // 2

    # Deterministic *and* series-diverse: round-robin across RTO codes so the
    # fixture spans MH04/MH43/MH06/GJ01/KA01/TN22/KL07/BH rather than clustering
    # on whichever prefix happens to sort first.
    by_rto: Dict[str, List[str]] = {}
    for plate in sorted(dataset):
        by_rto.setdefault(dataset[plate].rc.rto_code, []).append(plate)
    rtos = sorted(by_rto)
    order: List[str] = []
    idx = 0
    while len(order) < len(dataset):
        added = False
        for rto in rtos:
            bucket = by_rto[rto]
            if idx < len(bucket):
                order.append(bucket[idx])
                added = True
        if not added:
            break
        idx += 1

    for plate in order:
        rec = dataset[plate]
        issues = _issues_for(rec)
        entry = {
            "plate": plate,
            "rc_number": rec.rc.rc_number,
            "dl_number": rec.dl.dl_number,
            "expected_blacklist": rec.rc.blacklist_status.value,
            "expected_fastag_status": rec.fastag_status.value,
            "fitness_valid_to": rec.rc.fitness_valid_to.isoformat(),
            "fitness_expired": rec.rc.fitness_valid_to < REFERENCE_DATE,
            "issues": issues,
        }
        if issues and len(issue) < half:
            issue.append(entry)
        elif not issues and len(benign) < (n - half):
            benign.append(entry)
        if len(benign) >= (n - half) and len(issue) >= half:
            break

    return benign + issue


def _issues_for(rec: SeedRecord) -> List[str]:
    issues: List[str] = []
    if rec.rc.fitness_valid_to < REFERENCE_DATE:
        issues.append("expired_fitness")
    if rec.rc.blacklist_status is BlacklistStatus.BLACKLISTED:
        issues.append("blacklisted")
    if rec.fastag_status is FastagStatus.LOW_BALANCE:
        issues.append("fastag_low_balance")
    if rec.fastag_status is FastagStatus.BLACKLISTED:
        issues.append("fastag_blacklisted")
    return issues


def write_fixture(path: Path, dataset: Optional[Dict[str, SeedRecord]] = None, n: int = 50) -> dict:
    """Write the known-plates fixture and return the payload that was written."""
    dataset = dataset or generate_dataset()
    plates = select_known_plates(dataset, n=n)
    payload = {
        "seed": SEED,
        "reference_date": REFERENCE_DATE.isoformat(),
        "total_generated": len(dataset),
        "count": len(plates),
        "benign": sum(1 for p in plates if not p["issues"]),
        "with_issues": sum(1 for p in plates if p["issues"]),
        "plates": plates,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return payload


def _distribution(dataset: Dict[str, SeedRecord]) -> dict:
    n = len(dataset) or 1
    expired = sum(1 for r in dataset.values() if r.rc.fitness_valid_to < REFERENCE_DATE)
    black = sum(1 for r in dataset.values() if r.rc.blacklist_status is BlacklistStatus.BLACKLISTED)
    flow = sum(1 for r in dataset.values() if r.fastag_status is FastagStatus.LOW_BALANCE)
    fblack = sum(1 for r in dataset.values() if r.fastag_status is FastagStatus.BLACKLISTED)
    return {
        "total": n,
        "expired_fitness_pct": round(100 * expired / n, 2),
        "blacklisted_pct": round(100 * black / n, 2),
        "fastag_low_pct": round(100 * flow / n, 2),
        "fastag_blacklisted_pct": round(100 * fblack / n, 2),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the Vahan sim fixture.")
    ap.add_argument("--out", default="data/fixtures/known_plates.json",
                    help="fixture output path")
    ap.add_argument("--total", type=int, default=TOTAL_PLATES)
    ap.add_argument("--n", type=int, default=50, help="plates to write to fixture")
    args = ap.parse_args(list(argv) if argv is not None else None)

    dataset = generate_dataset(args.total)
    dist = _distribution(dataset)
    payload = write_fixture(Path(args.out), dataset, n=args.n)
    print(json.dumps({"fixture": args.out, "distribution": dist,
                      "fixture_summary": {k: payload[k] for k in
                                          ("count", "benign", "with_issues")}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
