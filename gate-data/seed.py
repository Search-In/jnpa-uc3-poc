"""Deterministic gate-data dataset generator for the JNPA UC-III PoC.

Implements the capture side of Appendix C requirements #4 and #5: for each
export container/vehicle pair the gate captures four source records that the
Auto-LEO (Let Export Order) process reconciles:

    * e-seal       -> (eseal_id, container_no, status, tamper_flag)
    * Form 13      -> (form13_no, container_no, shipping_bill_no, cargo_desc, gross_wt_kg)
    * weighbridge  -> (vehicle_plate, container_no, measured_wt_kg, axle_count)
    * ICEGATE      -> (shipping_bill_no, leo_status, igm_no, assessment)

Everything is generated *fully deterministically* from a fixed ``SEED`` and
anchored to a fixed ``REFERENCE_DATE`` (not "today"), so the demo, the dashboard
and the tests are reproducible run-to-run and host-to-host.

Container numbers are check-digit-VALID ISO 6346 (owner + 'U' + 6-digit serial +
computed check digit, e.g. ``MSCU1234566``), validated by
``jnpa_shared.iso6346``. Vehicle plates reuse the canonical Indian-plate format
from ``jnpa_shared`` so the gate data joins cleanly against the Vahan dataset.

A controlled slice of records deliberately MISMATCHES so the Customs flags fire:
    * ~6 %  e-seal tamper                 -> ESEAL_TAMPER
    * ~7 %  weighbridge vs Form-13 weight -> WEIGHT_MISMATCH
    * ~5 %  ICEGATE LEO not yet granted   -> LEO_MISSING

This module is importable (the FastAPI app builds its in-memory store from
``generate_dataset()``) and runnable as a script to print the distribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from jnpa_shared.iso6346 import with_check_digit

# --- Determinism anchors -----------------------------------------------------
SEED = "jnpa-uc3-gate-data-v1"
REFERENCE_DATE = date(2026, 6, 13)  # anchor for capture timestamps / SB dates
TOTAL_CONTAINERS = 200

# Shipping-line owner codes (the 3-letter prefix of an ISO 6346 container no,
# the 4th letter is always the category identifier 'U' for freight containers).
LINE_CODES = ["MSC", "MAE", "HLC", "CMA", "OOL", "EMC", "APL", "COS", "ONE", "ZIM"]

# Plausible export cargo descriptions (the Form-13 free-text field).
CARGO_DESCS = [
    "COTTON YARN",
    "ENGINEERING GOODS",
    "PHARMACEUTICAL FORMULATIONS",
    "BASMATI RICE",
    "LEATHER FOOTWEAR",
    "CERAMIC TILES",
    "AUTO COMPONENTS",
    "ORGANIC CHEMICALS",
    "MARINE PRODUCTS (FROZEN)",
    "READYMADE GARMENTS",
]

# ICEGATE assessment outcomes.
ASSESSMENTS = ["FACILITATED", "FACILITATED", "FACILITATED", "ASSESSED", "QUERY_RAISED"]

# RTO series the gate vehicles draw from (mirrors the Vahan sim series so the
# vehicle_plate joins line up with the Vahan dataset).
SERIES = ["MH04", "MH43", "MH06", "GJ01", "KA01", "TN22", "KL07"]
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"  # drop I/O/Q to avoid digit confusion


def _h(*parts: object) -> int:
    """Stable 64-bit-ish int hash from SEED + parts (not Python's salted hash)."""
    raw = SEED + "|" + "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big")


def _pick(seq, *parts) -> object:
    return seq[_h(*parts) % len(seq)]


def _pct(value: int, *parts) -> bool:
    """True for roughly ``value``% of inputs, deterministically."""
    return (_h("pct", *parts) % 10_000) < value * 100


# --- Source-record dataclasses ----------------------------------------------
@dataclass(frozen=True)
class EsealRecord:
    """An electronic seal (e-seal) reading captured at the export gate."""

    eseal_id: str
    container_no: str
    status: str          # ARMED | DISARMED | TAMPERED
    tamper_flag: bool
    captured_at: str     # ISO-8601 UTC


@dataclass(frozen=True)
class Form13Record:
    """A Form 13 (export goods registration) record."""

    form13_no: str
    container_no: str
    shipping_bill_no: str
    cargo_desc: str
    gross_wt_kg: int


@dataclass(frozen=True)
class WeighbridgeRecord:
    """A weighbridge reading tying a vehicle plate to a container weight."""

    vehicle_plate: str
    container_no: str
    measured_wt_kg: int
    axle_count: int
    captured_at: str     # ISO-8601 UTC


@dataclass(frozen=True)
class IcegateRecord:
    """An ICEGATE message: LEO / assessment status for a shipping bill."""

    shipping_bill_no: str
    container_no: str
    leo_status: str      # GRANTED | PENDING
    igm_no: str
    assessment: str


@dataclass(frozen=True)
class GateRecord:
    """All four captured source records for one container/vehicle pair."""

    container_no: str
    vehicle_plate: str
    eseal: EsealRecord
    form13: Form13Record
    weighbridge: WeighbridgeRecord
    icegate: IcegateRecord


# --- Field generators --------------------------------------------------------
def _container_no(i: int) -> str:
    """Build a check-digit-VALID ISO 6346 container number: 3 owner letters +
    'U' category + 6-digit serial + computed check digit (11 chars total)."""
    line = LINE_CODES[_h("line", i) % len(LINE_CODES)]
    serial = _h("cserial", i) % 1_000_000           # 000000..999999 (6 digits)
    return with_check_digit(f"{line}U{serial:06d}")


def _vehicle_plate(i: int) -> str:
    """Build a canonical Indian plate (SS DD L L NNNN) for the haulage truck."""
    prefix = SERIES[_h("vseries", i) % len(SERIES)]
    l1 = LETTERS[_h("vl1", i) % len(LETTERS)]
    l2 = LETTERS[_h("vl2", i) % len(LETTERS)]
    num = (_h("vnum", i) % 9999) + 1                 # 0001..9999
    return f"{prefix}{l1}{l2}{num:04d}"


def _shipping_bill_no(i: int) -> str:
    """A 7-digit shipping-bill number (ICEGATE SB serial)."""
    return f"{(_h('sb', i) % 9_000_000) + 1_000_000:07d}"


def _ts(i: int, *parts) -> str:
    """A deterministic capture timestamp on the reference date (ISO-8601 UTC)."""
    secs = _h("ts", i, *parts) % 86_400
    base = datetime(REFERENCE_DATE.year, REFERENCE_DATE.month, REFERENCE_DATE.day,
                    tzinfo=timezone.utc)
    return (base + timedelta(seconds=secs)).isoformat()


def _build_record(i: int) -> GateRecord:
    container_no = _container_no(i)
    vehicle_plate = _vehicle_plate(i)
    shipping_bill_no = _shipping_bill_no(i)
    cargo_desc = CARGO_DESCS[_h("cargo", i) % len(CARGO_DESCS)]

    # --- Anomaly draws (the slices that make Customs flags fire) -------------
    tampered = _pct(6, "tamper", i)
    weight_mismatch = _pct(7, "wmis", i)
    leo_missing = _pct(5, "leomiss", i)

    # --- e-seal --------------------------------------------------------------
    eseal_id = f"ESL{_h('eseal', i) % 10**10:010d}"
    eseal = EsealRecord(
        eseal_id=eseal_id,
        container_no=container_no,
        status="TAMPERED" if tampered else "ARMED",
        tamper_flag=tampered,
        captured_at=_ts(i, "eseal"),
    )

    # --- Form 13 -------------------------------------------------------------
    # Form-13 declared gross weight: laden container 12,000..30,000 kg.
    gross_wt_kg = 12_000 + _h("gross", i) % 18_001
    form13 = Form13Record(
        form13_no=f"F13{_h('f13', i) % 10**9:09d}",
        container_no=container_no,
        shipping_bill_no=shipping_bill_no,
        cargo_desc=cargo_desc,
        gross_wt_kg=gross_wt_kg,
    )

    # --- weighbridge ---------------------------------------------------------
    # Honest reading sits within +/-1% of the Form-13 gross (well inside the 2%
    # tolerance). A mismatch draw pushes it 4..9% off so WEIGHT_MISMATCH fires.
    if weight_mismatch:
        # Deterministic sign + magnitude of the discrepancy.
        sign = 1 if _h("wsign", i) % 2 == 0 else -1
        delta_pct = 4.0 + (_h("wdelta", i) % 500) / 100.0     # 4.00 .. 9.00 %
        measured_wt_kg = int(round(gross_wt_kg * (1 + sign * delta_pct / 100.0)))
    else:
        sign = 1 if _h("wsign", i) % 2 == 0 else -1
        delta_pct = (_h("wdelta", i) % 100) / 100.0           # 0.00 .. 1.00 %
        measured_wt_kg = int(round(gross_wt_kg * (1 + sign * delta_pct / 100.0)))

    weighbridge = WeighbridgeRecord(
        vehicle_plate=vehicle_plate,
        container_no=container_no,
        measured_wt_kg=measured_wt_kg,
        axle_count=_pick([4, 5, 6], "axle", i),
        captured_at=_ts(i, "wb"),
    )

    # --- ICEGATE -------------------------------------------------------------
    icegate = IcegateRecord(
        shipping_bill_no=shipping_bill_no,
        container_no=container_no,
        leo_status="PENDING" if leo_missing else "GRANTED",
        igm_no=f"IGM{_h('igm', i) % 10**7:07d}",
        assessment="QUERY_RAISED" if leo_missing else str(_pick(ASSESSMENTS, "assess", i)),
    )

    return GateRecord(
        container_no=container_no,
        vehicle_plate=vehicle_plate,
        eseal=eseal,
        form13=form13,
        weighbridge=weighbridge,
        icegate=icegate,
    )


# Well-known containers that must always resolve so the documented verification
# commands and the dashboard demo work as written. Each is forced onto a clean
# (no-flag) index so the "happy path" container always reconciles ready.
PINNED_CLEAN = "MSCU1234566"      # valid ISO6346; guaranteed leo_ready=True, zero flags
PINNED_TAMPER = "MAEU7654320"     # valid ISO6346; guaranteed ESEAL_TAMPER + leo_ready=False


def _clean_index() -> int:
    """Find a deterministic index whose draws are all clean (no anomalies)."""
    i = 0
    while True:
        if not (_pct(6, "tamper", i) or _pct(7, "wmis", i) or _pct(5, "leomiss", i)):
            return i
        i += 1


def _tamper_index() -> int:
    """Find a deterministic index whose only anomaly draw is the e-seal tamper."""
    i = 0
    while True:
        if _pct(6, "tamper", i) and not _pct(7, "wmis", i) and not _pct(5, "leomiss", i):
            return i
        i += 1


def _build_record_for_container(container_no: str, i: int) -> GateRecord:
    """Build a record with all deterministic attributes of index ``i`` but on a
    forced (well-known) container number."""
    base = _build_record(i)
    eseal = EsealRecord(
        eseal_id=base.eseal.eseal_id, container_no=container_no,
        status=base.eseal.status, tamper_flag=base.eseal.tamper_flag,
        captured_at=base.eseal.captured_at,
    )
    form13 = Form13Record(
        form13_no=base.form13.form13_no, container_no=container_no,
        shipping_bill_no=base.form13.shipping_bill_no, cargo_desc=base.form13.cargo_desc,
        gross_wt_kg=base.form13.gross_wt_kg,
    )
    weighbridge = WeighbridgeRecord(
        vehicle_plate=base.vehicle_plate, container_no=container_no,
        measured_wt_kg=base.weighbridge.measured_wt_kg, axle_count=base.weighbridge.axle_count,
        captured_at=base.weighbridge.captured_at,
    )
    icegate = IcegateRecord(
        shipping_bill_no=base.icegate.shipping_bill_no, container_no=container_no,
        leo_status=base.icegate.leo_status, igm_no=base.icegate.igm_no,
        assessment=base.icegate.assessment,
    )
    return GateRecord(
        container_no=container_no, vehicle_plate=base.vehicle_plate,
        eseal=eseal, form13=form13, weighbridge=weighbridge, icegate=icegate,
    )


def generate_dataset(total: int = TOTAL_CONTAINERS) -> Dict[str, GateRecord]:
    """Build the full deterministic dataset keyed by container number.

    Collisions (two indices producing the same container number) are rare but
    possible; later indices win, so the count may be marginally below ``total``.
    We backfill to guarantee at least ``total`` distinct containers. A couple of
    well-known containers (PINNED_*) are always injected so the documented
    verification commands and the demo's happy/unhappy paths resolve.
    """
    out: Dict[str, GateRecord] = {}
    i = 0
    while len(out) < total:
        rec = _build_record(i)
        out[rec.container_no] = rec
        i += 1
        if i > total * 4:  # safety valve; should never trigger
            break

    # Inject the pinned containers onto known-clean / known-tampered indices.
    out[PINNED_CLEAN] = _build_record_for_container(PINNED_CLEAN, _clean_index())
    out[PINNED_TAMPER] = _build_record_for_container(PINNED_TAMPER, _tamper_index())
    return out


# ---------------------------------------------------------------------------
# Script entrypoint — print the anomaly distribution of the dataset.
# ---------------------------------------------------------------------------
def _distribution(dataset: Dict[str, GateRecord]) -> dict:
    n = len(dataset) or 1
    tamper = sum(1 for r in dataset.values() if r.eseal.tamper_flag)
    leo_missing = sum(1 for r in dataset.values() if r.icegate.leo_status == "PENDING")
    # Weight mismatch recomputed from the captured records (the same maths the
    # reconciler uses with the default 2% tolerance).
    wmis = 0
    for r in dataset.values():
        gross = r.form13.gross_wt_kg or 1
        if abs(r.weighbridge.measured_wt_kg - gross) / gross * 100.0 > 2.0:
            wmis += 1
    return {
        "total": n,
        "eseal_tamper_pct": round(100 * tamper / n, 2),
        "weight_mismatch_pct": round(100 * wmis / n, 2),
        "leo_missing_pct": round(100 * leo_missing / n, 2),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the gate-data dataset.")
    ap.add_argument("--total", type=int, default=TOTAL_CONTAINERS)
    args = ap.parse_args(list(argv) if argv is not None else None)

    dataset = generate_dataset(args.total)
    print(json.dumps({"seed": SEED, "distribution": _distribution(dataset)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
