"""Realistic FASTag demo dataset — the single source of truth for demo data.

WHY THIS EXISTS
---------------
``demo_provider`` previously returned one flat, vehicle-agnostic payload
(``DEMOFASTAG001`` / ``DEMO_BANK`` / ``850.00`` for every RC, one transaction
dated "now"). Every vehicle looked identical, the Journey tab had a single dot,
and the transaction batch was mis-shaped so the mapper produced **zero** rows —
which is why the FASTag screen rendered empty cards.

This module replaces that with a deterministic, plausible operational history for
a named fleet of 10 vehicles: an account per vehicle, 5-10 toll crossings spread
over the last 30 days, and the completed trips those crossings belong to.

TWO CONSUMERS, ONE DATASET
--------------------------
* :mod:`services.fastag.demo_provider` serves it as a *vendor-shaped* payload, so
  ``/api/fastag/*`` in demo mode exercises the real
  ``client -> mapper -> service -> RDS`` pipeline.
* ``scripts/seed_fastag_demo.py`` pushes the same rows into
  ``core.fastag_balance`` / ``core.fastag_transaction`` / ``core.toll_enroute``,
  so the RDS-backed views (``GET /api/fastag/transactions/history``, the Journey
  timeline) are populated without any vendor call.

Both read from here, so the persisted history and the live demo fetch agree
exactly — same tag ids, same ``seq_no``, same plazas.

DETERMINISM
-----------
Every value is derived from a CRC-32 of the plate (never :func:`hash`, which is
salted per process, and never :mod:`random` at module scope). Crossing times are
relative to "now" so the demo always looks current, but ``seq_no`` is derived
from the plate and the crossing's ordinal alone — so re-seeding, or refreshing
the screen a week later, collides on ``ON CONFLICT (seq_no) DO NOTHING`` and the
persisted history stays pinned at the 5-10 crossings originally written.

SCHEMA NOTES (no migration is implied by this file)
---------------------------------------------------
``core.fastag_transaction`` has no ``amount`` column, so a crossing's fare is not
persisted per row. Fares live on the plaza catalogue below, are emitted on the
vendor transaction payload (where the mapper reports them via
``unmapped_fields`` — the designed signal for a vendor field we do not model),
and *are* persisted per plaza in ``core.toll_enroute.toll_plaza_details[].cost``.
A crossing's fare is therefore always recoverable from its plaza name.

Coordinates are demo-grade (plaza vicinity, not surveyed positions).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from zlib import crc32

# --------------------------------------------------------------------------- config
#: The demo fleet. Any RC outside this list still resolves (see :func:`profile_for`)
#: so the screen never renders an empty card for an unknown plate.
SEED_PLATES: tuple[str, ...] = (
    "MH04AB1234",
    "MH04LZ1507",
    "MH12AB1234",
    "MH43RU6076",
    "MH06YC5513",
    "GJ01FH4584",
    "KA01SU7736",
    "TN22EN8965",
    "KL07TF1494",
    "MH04SP0569",
)

#: Balance band required by the demo spec (rupees).
BALANCE_MIN = Decimal("500.00")
BALANCE_MAX = Decimal("5000.00")

#: Toll crossings per vehicle over the trailing 30-day window.
TXN_MIN, TXN_MAX = 5, 10

#: How far back crossings may be dated.
WINDOW_DAYS = 30

#: The most recent crossing is never older than this, so "last seen" reads live
#: on the Health view and today's/this-week's counters are non-zero.
MAX_LAST_SEEN_HOURS = 30


# --------------------------------------------------------------------------- plazas
class Plaza:
    """One toll plaza: display name, position, and the fare by vehicle class.

    ``fares`` is keyed by the NETC vehicle-class digit used on the account
    (``"6"`` 2-axle, ``"7"`` 3-axle, ``"8"`` 4-6 axle), because a plaza charges a
    3-axle truck more than a 2-axle one — a flat fare would read as fake.
    """

    __slots__ = ("name", "lat", "lng", "fares")

    def __init__(self, name: str, lat: float, lng: float, fares: dict[str, str]) -> None:
        self.name = name
        self.lat = lat
        self.lng = lng
        self.fares = fares

    def fare(self, vehicle_class: str) -> Decimal:
        return Decimal(self.fares.get(vehicle_class, next(iter(self.fares.values()))))

    @property
    def geocode(self) -> str:
        """Raw ``"lat,lng"`` exactly as the vendor sends it (the mapper splits it)."""
        return f"{self.lat},{self.lng}"


def _P(name: str, lat: float, lng: float, f6: str, f7: str, f8: str) -> Plaza:
    return Plaza(name, lat, lng, {"6": f6, "7": f7, "8": f8})


#: Plazas on the corridors that actually feed JNPA (Nhava Sheva). Fares are
#: single-journey commercial rates, rounded to the ₹5 steps NHAI publishes.
PLAZAS: dict[str, Plaza] = {p_key: p for p_key, p in {
    # --- Navi Mumbai / port approach ---
    "jnpa":        _P("JNPA Nhava Sheva Toll Plaza", 18.9490, 72.9510, "115.00", "175.00", "250.00"),
    "palaspe":     _P("Palaspe Phata Toll Plaza", 18.9990, 73.1070, "95.00", "145.00", "205.00"),
    "vashi":       _P("Vashi Toll Naka", 19.0700, 72.9990, "80.00", "120.00", "170.00"),
    "airoli":      _P("Airoli Toll Naka", 19.1550, 72.9980, "80.00", "120.00", "170.00"),
    # --- Mumbai-Pune Expressway / NH-48 south-east ---
    "khalapur":    _P("Khalapur Toll Plaza", 18.7950, 73.3090, "310.00", "465.00", "660.00"),
    "talegaon":    _P("Talegaon Toll Plaza", 18.7170, 73.6640, "295.00", "445.00", "630.00"),
    # --- NH-160 / NH-848 north-east (Nashik) ---
    "vadape":      _P("Vadape Toll Plaza", 19.3250, 73.1130, "140.00", "210.00", "300.00"),
    "khardi":      _P("Khardi Toll Plaza", 19.5540, 73.3950, "155.00", "235.00", "330.00"),
    # --- NH-48 north (Gujarat) ---
    "achhad":      _P("Achhad Toll Plaza", 19.7840, 72.9300, "185.00", "280.00", "395.00"),
    "charoti":     _P("Charoti Toll Plaza", 19.9370, 72.8990, "190.00", "285.00", "405.00"),
    "bhilad":      _P("Bhilad Toll Plaza", 20.3120, 72.9300, "175.00", "265.00", "375.00"),
    "kherva":      _P("Kherva Toll Plaza", 22.6030, 72.7690, "165.00", "250.00", "355.00"),
    # --- NH-48 south (Karnataka / Kerala) ---
    "anewadi":     _P("Anewadi Toll Plaza", 17.8920, 73.9680, "200.00", "300.00", "425.00"),
    "tasawade":    _P("Tasawade Toll Plaza", 17.3980, 74.1850, "195.00", "295.00", "415.00"),
    "kini":        _P("Kini Toll Plaza", 16.8770, 74.1280, "190.00", "285.00", "405.00"),
    "kagal":       _P("Kagal Toll Plaza", 16.5790, 74.3180, "185.00", "280.00", "395.00"),
    "hattargi":    _P("Hattargi Toll Plaza", 16.2670, 74.4800, "180.00", "270.00", "385.00"),
    "nelamangala": _P("Nelamangala Toll Plaza", 13.0960, 77.3950, "170.00", "255.00", "365.00"),
    "paliyekkara": _P("Paliyekkara Toll Plaza", 10.3540, 76.3310, "160.00", "240.00", "340.00"),
    # --- NH-44 / NH-48 south-east (Tamil Nadu) ---
    "walajahpet":  _P("Walajahpet Toll Plaza", 12.9300, 79.3600, "165.00", "250.00", "355.00"),
    "krishnagiri": _P("Krishnagiri Toll Plaza", 12.5170, 78.2120, "175.00", "265.00", "375.00"),
}.items()}


class Corridor:
    """A freight lane out of JNPA: the plazas on it, plus trip metadata.

    ``plaza_keys`` is ordered *outbound* (port first). The return leg replays it
    reversed, which is what makes the Journey timeline read as round trips rather
    than a random scatter of crossings.
    """

    __slots__ = ("key", "dest_name", "dest_state", "plaza_keys", "distance_km",
                 "duration", "bearing")

    def __init__(self, key: str, dest_name: str, dest_state: str,
                 plaza_keys: tuple[str, ...], distance_km: str, duration: str,
                 bearing: tuple[str, str]) -> None:
        self.key = key
        self.dest_name = dest_name
        self.dest_state = dest_state
        self.plaza_keys = plaza_keys
        self.distance_km = distance_km
        self.duration = duration
        #: (outbound, return) NETC lane-direction codes.
        self.bearing = bearing

    def plazas(self) -> list[Plaza]:
        return [PLAZAS[k] for k in self.plaza_keys]


SOURCE_NAME = "Nhava Sheva"
SOURCE_STATE = "Maharashtra"

CORRIDORS: dict[str, Corridor] = {
    c.key: c for c in (
        Corridor("mumbai", "Mumbai", "Maharashtra",
                 ("jnpa", "vashi", "airoli"), "62.40", "1h 45m", ("N", "S")),
        Corridor("pune", "Pune", "Maharashtra",
                 ("jnpa", "palaspe", "khalapur", "talegaon"), "148.50", "3h 20m", ("E", "W")),
        Corridor("nashik", "Nashik", "Maharashtra",
                 ("jnpa", "vashi", "vadape", "khardi"), "182.70", "4h 05m", ("N", "S")),
        Corridor("ahmedabad", "Ahmedabad", "Gujarat",
                 ("jnpa", "achhad", "charoti", "bhilad", "kherva"), "534.80", "10h 30m", ("N", "S")),
        Corridor("bengaluru", "Bengaluru", "Karnataka",
                 ("jnpa", "palaspe", "anewadi", "kini", "hattargi", "nelamangala"),
                 "982.60", "18h 40m", ("S", "N")),
        Corridor("chennai", "Chennai", "Tamil Nadu",
                 ("jnpa", "palaspe", "tasawade", "kagal", "krishnagiri", "walajahpet"),
                 "1338.20", "25h 15m", ("S", "N")),
        Corridor("kochi", "Kochi", "Kerala",
                 ("jnpa", "palaspe", "kini", "hattargi", "paliyekkara"),
                 "1256.90", "23h 50m", ("S", "N")),
    )
}


# --------------------------------------------------------------------------- fleet
class VehicleProfile:
    """Everything the three FASTag APIs need to describe one vehicle."""

    __slots__ = ("rc_number", "customer_name", "provider_name", "provider_code",
                 "vehicle_class", "vehicle_class_desc", "model_name", "corridor_key",
                 "toll_vehicle_type")

    def __init__(self, rc_number: str, customer_name: str, provider_name: str,
                 provider_code: str, vehicle_class: str, model_name: Optional[str],
                 corridor_key: str, toll_vehicle_type: str) -> None:
        self.rc_number = rc_number
        self.customer_name = customer_name
        self.provider_name = provider_name
        self.provider_code = provider_code
        self.vehicle_class = vehicle_class
        self.vehicle_class_desc = VEHICLE_CLASS_DESC[vehicle_class]
        self.model_name = model_name
        self.corridor_key = corridor_key
        #: Class accepted by ``POST /api/fastag/toll-enroute`` (gateway VEHICLE_TYPES).
        self.toll_vehicle_type = toll_vehicle_type

    @property
    def corridor(self) -> Corridor:
        return CORRIDORS[self.corridor_key]

    @property
    def netc_vehicle_type(self) -> str:
        """NETC class code as it appears on a crossing row, e.g. ``"VC7"``."""
        return f"VC{self.vehicle_class}"


VEHICLE_CLASS_DESC: dict[str, str] = {
    "6": "Bus / Truck (2 Axle)",
    "7": "3-Axle Commercial Vehicle",
    "8": "Truck (4, 5, 6 Axle) / HCM / EME",
}

#: Issuer banks, with the NETC-style acquirer code prefix each uses.
_BANKS: tuple[tuple[str, str], ...] = (
    ("HDFC Bank", "HDFC"),
    ("ICICI Bank", "ICIC"),
    ("Axis Bank", "UTIB"),
    ("IDFC FIRST Bank", "IDFB"),
    ("Paytm Payments Bank", "PYTM"),
    ("Kotak Mahindra Bank", "KKBK"),
    ("State Bank of India", "SBIN"),
    ("Bank of Baroda", "BARB"),
    ("Federal Bank", "FDRL"),
    ("IndusInd Bank", "INDB"),
)

_FLEET: tuple[VehicleProfile, ...] = (
    VehicleProfile("MH04AB1234", "SHREE SAI ROADLINES", "HDFC Bank", "HDFC30011TRKMH",
                   "7", "TATA SIGNA 3118.T", "pune", "TRUCK"),
    VehicleProfile("MH04LZ1507", "KONKAN CARGO MOVERS", "ICICI Bank", "ICIC44027TRKMH",
                   "6", "ASHOK LEYLAND 1616", "mumbai", "TRUCK"),
    VehicleProfile("MH12AB1234", "DECCAN CONTAINER LINES", "Axis Bank", "UTIB51830TRKMH",
                   "7", "BHARATBENZ 3128C", "pune", "TRUCK"),
    VehicleProfile("MH43RU6076", "NAVI MUMBAI TRANSPORT CO", "IDFC FIRST Bank", "IDFB88000TRKMH",
                   "6", "EICHER PRO 3019", "nashik", "TRUCK"),
    VehicleProfile("MH06YC5513", "RAIGAD BULK CARRIERS", "Paytm Payments Bank", "PYTM60219TRKMH",
                   "8", "VOLVO FM 460", "mumbai", "MAV"),
    VehicleProfile("GJ01FH4584", "SABARMATI FREIGHT LOGISTICS", "Kotak Mahindra Bank",
                   "KKBK17742TRKGJ", "8", "TATA PRIMA 4028.S", "ahmedabad", "MAV"),
    VehicleProfile("KA01SU7736", "NANDI EXPRESS CARGO", "State Bank of India", "SBIN29604TRKKA",
                   "7", "ASHOK LEYLAND 3120", "bengaluru", "TRUCK"),
    VehicleProfile("TN22EN8965", "COROMANDEL HAULAGE PVT LTD", "Bank of Baroda", "BARB73155TRKTN",
                   "8", "SCANIA R 500", "chennai", "MAV"),
    VehicleProfile("KL07TF1494", "MALABAR CONTAINER SERVICES", "Federal Bank", "FDRL39481TRKKL",
                   "6", "MAHINDRA BLAZO X 28", "kochi", "TRUCK"),
    VehicleProfile("MH04SP0569", "THANE PORT CARRIERS", "IndusInd Bank", "INDB20866TRKMH",
                   "7", "TATA SIGNA 4225.TK", "pune", "TRUCK"),
)

FLEET: dict[str, VehicleProfile] = {v.rc_number: v for v in _FLEET}


# --------------------------------------------------------------------- determinism
def _seed(rc_number: str, salt: str = "") -> int:
    """Stable 32-bit seed for a plate. CRC-32, never :func:`hash` (PYTHONHASHSEED)."""
    return crc32(f"{rc_number}|{salt}".encode("utf-8"))


def _rng(rc_number: str, salt: str = "") -> random.Random:
    return random.Random(_seed(rc_number, salt))


def normalize_rc(rc_number: Optional[str]) -> str:
    return "".join(str(rc_number or "").split()).upper()


def profile_for(rc_number: str) -> VehicleProfile:
    """The profile for ``rc_number``, synthesising one for an unlisted plate.

    An unknown plate must never produce an empty card — an operator searching a
    vehicle outside the demo fleet still gets a coherent, stable account rather
    than a 502 or a blank screen. The synthesised profile is derived from the
    plate, so it is identical on every lookup and on every process.
    """
    rc = normalize_rc(rc_number)
    known = FLEET.get(rc)
    if known is not None:
        return known

    rng = _rng(rc, "profile")
    bank_name, bank_code = _BANKS[rng.randrange(len(_BANKS))]
    corridor_key = sorted(CORRIDORS)[rng.randrange(len(CORRIDORS))]
    vehicle_class = ("6", "7", "8")[rng.randrange(3)]
    state = rc[:2] if len(rc) >= 2 else "MH"
    return VehicleProfile(
        rc_number=rc or "UNKNOWN",
        customer_name=f"{state} FREIGHT OPERATOR {rng.randrange(100, 999)}",
        provider_name=bank_name,
        provider_code=f"{bank_code}{rng.randrange(10000, 99999)}TRK{state}",
        vehicle_class=vehicle_class,
        model_name=None,
        corridor_key=corridor_key,
        toll_vehicle_type="MAV" if vehicle_class == "8" else "TRUCK",
    )


def tag_id_for(rc_number: str) -> str:
    """A stable 24-char NETC-style EPC tag id (``3416`` issuer prefix + payload)."""
    rc = normalize_rc(rc_number)
    rng = _rng(rc, "tag")
    return "3416" + "".join(rng.choice("0123456789ABCDEF") for _ in range(20))


def balance_for(rc_number: str) -> Decimal:
    """Available balance in the required ₹500-₹5000 band, in ₹0.50 steps."""
    rng = _rng(normalize_rc(rc_number), "balance")
    steps = int((BALANCE_MAX - BALANCE_MIN) / Decimal("0.50"))
    return (BALANCE_MIN + Decimal(rng.randrange(steps + 1)) * Decimal("0.50")).quantize(
        Decimal("0.01")
    )


def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


# ------------------------------------------------------------------------- trips
class Trip:
    """One completed leg: an ordered run of crossings along a corridor.

    ``core.toll_enroute`` has no per-trip status column, so "completed" is not a
    stored flag — it is the fact that the leg's full plaza sequence has SUCCESS
    crossings on record. :attr:`completed` states that explicitly for callers.
    """

    __slots__ = ("profile", "outbound", "started_at", "crossings")

    def __init__(self, profile: VehicleProfile, outbound: bool, started_at: datetime,
                 crossings: list[dict[str, Any]]) -> None:
        self.profile = profile
        self.outbound = outbound
        self.started_at = started_at
        self.crossings = crossings

    @property
    def origin(self) -> tuple[str, str]:
        c = self.profile.corridor
        return (SOURCE_NAME, SOURCE_STATE) if self.outbound else (c.dest_name, c.dest_state)

    @property
    def destination(self) -> tuple[str, str]:
        c = self.profile.corridor
        return (c.dest_name, c.dest_state) if self.outbound else (SOURCE_NAME, SOURCE_STATE)

    @property
    def plazas(self) -> list[Plaza]:
        seq = self.profile.corridor.plazas()
        return seq if self.outbound else list(reversed(seq))

    @property
    def tolls_crossed(self) -> int:
        return len(self.crossings)

    @property
    def total_toll(self) -> Decimal:
        return sum((Decimal(c["amount"]) for c in self.crossings), Decimal("0.00"))

    @property
    def completed(self) -> bool:
        """True once every plaza on the leg has been crossed successfully."""
        return (
            len(self.crossings) == len(self.profile.corridor.plaza_keys)
            and all(c["status"] == "SUCCESS" for c in self.crossings)
        )


def _seq_no(rc_number: str, index: int) -> str:
    """Stable numeric NETC-style sequence number — the vendor idempotency key.

    Deliberately derived from the plate and the crossing's ordinal ONLY, never
    from its timestamp. Crossing times are relative to "now", so a date-anchored
    seq_no would mint fresh keys every time the window rolled over a day
    boundary: each demo refresh would then append another near-duplicate batch
    and the stored history would creep past :data:`TXN_MAX`. Keyed this way, a
    refetch collides on ``ON CONFLICT (seq_no) DO NOTHING`` and the persisted
    count stays pinned at the seeded 5-10 no matter how often the screen is
    refreshed.
    """
    return f"{_seed(rc_number, 'seq') % 10**10:010d}{index:04d}"


def _leg_count(leg_len: int, target: int) -> int:
    """How many whole legs land the crossing count inside [TXN_MIN, TXN_MAX].

    Whole legs only: a half-driven corridor would make :attr:`Trip.completed`
    false for a trip that is meant to read as finished. Among the leg counts that
    fit the band, the one landing closest to ``target`` wins.
    """
    fits = [n for n in range(1, TXN_MAX + 1) if TXN_MIN <= n * leg_len <= TXN_MAX]
    if not fits:
        return 1
    return min(fits, key=lambda n: (abs(n * leg_len - target), n))


def trips_for(rc_number: str, *, now: Optional[datetime] = None) -> list[Trip]:
    """Completed trips for ``rc_number`` over the trailing 30 days, newest first.

    Legs are whole corridor runs (see :func:`_leg_count`) alternating outbound /
    return, laid down newest-first across the window. Total crossings always land
    in :data:`TXN_MIN`-:data:`TXN_MAX`, and the newest leg is always within
    :data:`MAX_LAST_SEEN_HOURS` — which keeps "last seen" live and today's
    counter honest.
    """
    rc = normalize_rc(rc_number)
    profile = profile_for(rc)
    corridor = profile.corridor
    ref = _now(now)
    rng = _rng(rc, "trips")

    leg_len = len(corridor.plaza_keys)
    legs = _leg_count(leg_len, rng.randint(TXN_MIN, TXN_MAX))

    # Newest leg first, then walk backwards through the window. Gaps are drawn per
    # leg so two vehicles on the same corridor do not move in lockstep.
    #
    # The newest leg starts a full driving-duration *before* its target last-seen
    # age: a Chennai run takes 25h, so anchoring it 2h ago would shove every
    # crossing on it forward onto "now" and collapse a two-day haul into one
    # timestamp. Anchoring by leg span keeps the last crossing inside
    # MAX_LAST_SEEN_HOURS while the earlier ones stay properly spread.
    leg_hours = max(1, round(_duration_minutes(corridor.duration) / 60))
    hours_ago = leg_hours + rng.randint(1, MAX_LAST_SEEN_HOURS)

    # Remaining legs are spread over the rest of the 30-day window rather than
    # bunched behind the newest one, so the history reads as a month of running
    # instead of three trips in one week. Jittered so two vehicles on the same
    # corridor don't share a schedule.
    window_h = WINDOW_DAYS * 24
    gap_h = max(30.0, (window_h - hours_ago) / max(1, legs))

    trips: list[Trip] = []
    outbound = rng.random() < 0.5
    txn_index = 0

    for _ in range(legs):
        start = ref - timedelta(hours=hours_ago)
        if start < ref - timedelta(days=WINDOW_DAYS):
            break
        crossings: list[dict[str, Any]] = []
        plazas = corridor.plazas() if outbound else list(reversed(corridor.plazas()))
        # Crossings within a leg are spaced by the real driving time between
        # plazas, apportioned across the corridor's published duration.
        span_min = max(30, _duration_minutes(corridor.duration))
        step_min = span_min / max(1, leg_len - 1) if leg_len > 1 else 0
        for i, plaza in enumerate(plazas):
            when = start + timedelta(minutes=round(i * step_min) + rng.randint(0, 9))
            if when > ref:
                when = ref - timedelta(minutes=rng.randint(1, 30))
            crossings.append({
                "seq_no": _seq_no(rc, txn_index),
                "tag_id": tag_id_for(rc),
                "rc_number": rc,
                "transaction_date_time": when,
                "lane_direction": corridor.bearing[0 if outbound else 1],
                "toll_plaza_name": plaza.name,
                "toll_plaza_geocode": plaza.geocode,
                "vehicle_type": profile.netc_vehicle_type,
                "bank_name": profile.provider_name,
                "status": "SUCCESS",
                # Fare has no column on core.fastag_transaction (see module
                # docstring); it is carried here and persisted per plaza on
                # core.toll_enroute.
                "amount": str(plaza.fare(profile.vehicle_class)),
            })
            txn_index += 1
        trips.append(Trip(profile, outbound, start, crossings))
        outbound = not outbound                      # legs alternate: out, back, out…
        hours_ago += gap_h * rng.uniform(0.8, 1.2)   # jittered stride back in time

    return trips


def _duration_minutes(duration: str) -> int:
    """``"3h 20m"`` -> ``200``. Falls back to 120 for an unparseable value."""
    total, digits = 0, ""
    for ch in duration:
        if ch.isdigit():
            digits += ch
        elif ch == "h" and digits:
            total += int(digits) * 60
            digits = ""
        elif ch == "m" and digits:
            total += int(digits)
            digits = ""
    return total or 120


# ------------------------------------------------------------- vendor-shaped views
def account_payload(rc_number: str) -> dict[str, Any]:
    """The ``core.fastag_balance`` view of a vehicle, in vendor field names."""
    rc = normalize_rc(rc_number)
    p = profile_for(rc)
    return {
        "rc_number": rc,
        "tag_id": tag_id_for(rc),
        "provider_name": p.provider_name,
        "provider_code": p.provider_code,
        "customer_name": p.customer_name,
        # Recharge headroom is what is left under the ₹10,000 wallet cap.
        "available_recharge_limit": str(
            (Decimal("10000.00") - balance_for(rc)).quantize(Decimal("0.01"))
        ),
        "available_balance": str(balance_for(rc)),
        "tag_status": "ACTIVE",
        "vehicle_class": p.vehicle_class,
        "vehicle_class_desc": p.vehicle_class_desc,
        "model_name": p.model_name,
    }


def transactions_payload(rc_number: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """The transaction *batch* for a vehicle, newest crossing first.

    Shaped as ``FastagTransactionBatch`` expects — an object carrying batch-level
    ``rc_number``/``tag_id``/``bank_name``/``status`` plus a ``transactions``
    array. (The previous demo returned a bare list, which the mapper read as an
    unmapped ``data`` key and turned into zero rows.)
    """
    rc = normalize_rc(rc_number)
    p = profile_for(rc)
    rows = [c for trip in trips_for(rc, now=now) for c in trip.crossings]
    rows.sort(key=lambda r: r["transaction_date_time"], reverse=True)
    return {
        "rc_number": rc,
        "tag_id": tag_id_for(rc),
        "bank_name": p.provider_name,
        "status": "SUCCESS",
        "transactions": [
            {**r, "transaction_date_time": r["transaction_date_time"].isoformat()}
            for r in rows
        ],
    }


def journeys_payload(rc_number: str, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Completed trips as ``core.toll_enroute`` route lookups (newest first).

    One row per leg: origin, destination, distance, duration and the full plaza
    array with fares — which is where a trip's toll spend is actually persisted.
    """
    rc = normalize_rc(rc_number)
    p = profile_for(rc)
    out: list[dict[str, Any]] = []
    for trip in trips_for(rc, now=now):
        src_name, src_state = trip.origin
        dst_name, dst_state = trip.destination
        out.append({
            # client_id is the requesting-client marker; tagging it with the RC
            # keeps a seeded leg traceable back to its vehicle (the table has no
            # rc_number column).
            "client_id": f"demo-seed:{rc}",
            "source_name": src_name,
            "source_state": src_state,
            "destination_name": dst_name,
            "destination_state": dst_state,
            "vehicle_type": p.toll_vehicle_type,
            "duration": p.corridor.duration,
            "distance": p.corridor.distance_km,
            "toll_plaza_details": [
                {
                    "toll_plaza_name": plaza.name,
                    "cost": str(plaza.fare(p.vehicle_class)),
                    "toll_plaza_latitude": plaza.lat,
                    "toll_plaza_longitude": plaza.lng,
                }
                for plaza in trip.plazas
            ],
            # Derived, not columns — returned so callers can report the leg
            # without re-deriving it. See Trip.completed.
            "_started_at": trip.started_at,
            "_tolls_crossed": trip.tolls_crossed,
            "_total_toll": str(trip.total_toll),
            "_completed": trip.completed,
        })
    return out


def enroute_payload(
    source_state: str, source_name: str, destination_state: str,
    destination_name: str, vehicle_type: str,
) -> dict[str, Any]:
    """Toll plazas enroute for an arbitrary route request.

    Matches the request against the JNPA corridors by destination (then by
    origin, for a return leg) and falls back to the Pune corridor, so the form
    always answers with a real plaza list instead of an empty result.
    """
    corridor, reverse = _match_corridor(source_name, destination_name)
    vclass = "8" if str(vehicle_type).upper() in {"MAV", "HGV", "MMV"} else "7"
    plazas = corridor.plazas()
    if reverse:
        plazas = list(reversed(plazas))
    return {
        "sourceState": source_state,
        "sourceName": source_name,
        "destinationState": destination_state,
        "destinationName": destination_name,
        "vehicleType": vehicle_type,
        "duration": corridor.duration,
        "distance": f"{corridor.distance_km} km",
        "toll_plaza_details": [
            {
                "toll_plaza_name": p.name,
                "cost": str(p.fare(vclass)),
                "toll_plaza_latitude": p.lat,
                "toll_plaza_longitude": p.lng,
            }
            for p in plazas
        ],
    }


def _match_corridor(source_name: str, destination_name: str) -> tuple[Corridor, bool]:
    """Best corridor for a route, and whether the plaza order must be reversed."""
    src = str(source_name or "").strip().lower()
    dst = str(destination_name or "").strip().lower()
    for corridor in CORRIDORS.values():
        if corridor.dest_name.lower() in dst or dst in corridor.dest_name.lower():
            return corridor, False
    for corridor in CORRIDORS.values():
        if corridor.dest_name.lower() in src or src in corridor.dest_name.lower():
            return corridor, True
    return CORRIDORS["pune"], False


def health_payload(rc_number: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Per-vehicle tag health: is the tag active, when was it last seen, signal.

    Derived — ``GET /api/fastag/health`` reports *module* health and its contract
    is untouched. This is the vehicle-level view the same facts support:
    ``tag_status`` from the account, ``last_seen`` from the newest crossing, and a
    signal grade from how stale that crossing is (a tag that has not read at a
    plaza in days is what a weak or unread tag looks like in the field).
    """
    rc = normalize_rc(rc_number)
    ref = _now(now)
    crossings = [c for trip in trips_for(rc, now=now) for c in trip.crossings]
    last_seen = max((c["transaction_date_time"] for c in crossings), default=None)
    age_h = None if last_seen is None else (ref - last_seen).total_seconds() / 3600.0
    if age_h is None:
        signal = "NO_SIGNAL"
    elif age_h <= 24:
        signal = "STRONG"
    elif age_h <= 96:
        signal = "GOOD"
    else:
        signal = "WEAK"
    return {
        "rc_number": rc,
        "tag_id": tag_id_for(rc),
        "active": True,
        "tag_status": "ACTIVE",
        "last_seen": None if last_seen is None else last_seen.isoformat(),
        "last_seen_age_hours": None if age_h is None else round(age_h, 1),
        "signal_status": signal,
        "reads_30d": len(crossings),
    }


__all__ = [
    "SEED_PLATES", "FLEET", "CORRIDORS", "PLAZAS",
    "Corridor", "Plaza", "Trip", "VehicleProfile",
    "account_payload", "transactions_payload", "journeys_payload",
    "enroute_payload", "health_payload",
    "profile_for", "tag_id_for", "balance_for", "trips_for", "normalize_rc",
]
