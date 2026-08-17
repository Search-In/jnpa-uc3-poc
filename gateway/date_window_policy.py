"""Which list endpoints a date window does NOT belong on, and why.  GAP-DATE-01.

Most paginated GETs in this gateway should accept `?from_date=&to_date=`. Some
should not, and the difference is not obvious enough to leave to a heuristic —
an earlier pass classified endpoints by scanning for timestamp columns and got
several wrong in both directions:

  * it marked the weather feed N/A because `core.weather_reading` has only
    `created_at`. For a polled sensor, `created_at` IS the observation time, so
    a window there is entirely meaningful.
  * it marked `vehicles.available_vehicles` as needing one because the tables
    behind it carry timestamps. But "which vehicles are free" is a question
    about NOW; bounding it by date answers nothing.

So the exclusions are enumerated here, each with the reason, and reviewed by a
person. The ledger generator reads this file; anything not listed is expected to
carry a window, which means a new endpoint is treated as needing one until
someone says otherwise. That is the safe default: a missing filter is visible,
a silently pointless one is not.

Three grounds for exclusion, and nothing else counts:

  NOT_A_TIME_SERIES  the rows are things that exist, not things that happened —
                     master data, reference geometry, current availability.
  NO_UPSTREAM_FILTER the data comes from an external API we proxy, and that API
                     exposes no date parameter. Accepting one here would filter
                     a page we already fetched, which is worse than refusing:
                     it looks like a date filter and is a pagination artefact.
  NO_TIMESTAMP       the backing table records no time at all.
"""
from __future__ import annotations

from typing import Dict, Tuple

NOT_A_TIME_SERIES = "NOT_A_TIME_SERIES"
NO_UPSTREAM_FILTER = "NO_UPSTREAM_FILTER"
NO_TIMESTAMP = "NO_TIMESTAMP"

#: (router file, function) -> (ground, reason)
EXCLUSIONS: Dict[Tuple[str, str], Tuple[str, str]] = {
    # --- reference geometry and infrastructure ------------------------------
    ("bhuvan.py", "bhuvan_layers"): (
        NOT_A_TIME_SERIES,
        "WMS layer capabilities from the Bhuvan service — a catalogue of map "
        "layers, not records with dates."),
    ("gatishakti.py", "toll_plazas"): (
        NOT_A_TIME_SERIES,
        "Toll plaza locations. Infrastructure that exists; `created_at` is when "
        "we ingested the reference set."),
    ("gatishakti.py", "roads"): (
        NOT_A_TIME_SERIES, "Road segment geometry — reference data."),
    ("gatishakti.py", "nh_numbers"): (
        NOT_A_TIME_SERIES, "Distinct national-highway numbers — a lookup list."),
    ("gatishakti.py", "road_points"): (
        NOT_A_TIME_SERIES, "Road point geometry — reference data."),
    ("marine_sea_channel.py", "list_channels"): (
        NOT_A_TIME_SERIES, "The JNPA channel and reach definitions."),

    # --- master data --------------------------------------------------------
    ("transporters.py", "list_transporters"): (
        NOT_A_TIME_SERIES,
        "The transporter company master. All 2,191 rows share one `created_at` "
        "instant (2026-07-31 12:05:31) — bulk-loaded — so a window returns "
        "everything or nothing."),
    ("transporters.py", "active_blacklist"): (
        NOT_A_TIME_SERIES,
        "CURRENT blacklist state. History belongs to the blacklist event log, "
        "which is a different endpoint."),
    ("vehicle_registry.py", "list_mappings"): (
        NOT_A_TIME_SERIES, "Vehicle-to-transporter mappings — master data."),
    ("drivers_master.py", "list_drivers"): (
        NOT_A_TIME_SERIES,
        "The driver master. Measured on jnpa_schema_v3: all 31,846 rows share a "
        "single `created_at` instant (2026-07-31 14:54:50) because the PDP "
        "licence list was bulk-loaded, so a window returns everything or "
        "nothing. This endpoint WAS wired with one and it has been removed."),
    ("scenarios.py", "list_scenario_handles"): (
        NOT_A_TIME_SERIES,
        "Registered what-if scenario definitions — a catalogue of what CAN be "
        "run, not a log of runs."),
    ("workflows.py", "list_rules"): (
        NOT_A_TIME_SERIES, "Automation rule definitions."),

    # --- current state, not history -----------------------------------------
    ("vehicles.py", "available_vehicles"): (
        NOT_A_TIME_SERIES,
        "Which vehicles are assignable RIGHT NOW. 'Available between two dates' "
        "is a different question this endpoint does not answer."),
    ("identity.py", "available_drivers"): (
        NOT_A_TIME_SERIES, "Drivers currently free for assignment."),
    ("identity.py", "available_vehicles"): (
        NOT_A_TIME_SERIES, "Vehicles currently free for assignment."),
    ("empty_container.py", "containers_available"): (
        NOT_A_TIME_SERIES, "Current empty-container availability."),
    ("cargo.py", "scan_queue"): (
        NOT_A_TIME_SERIES,
        "The containers awaiting scan RIGHT NOW. A queue is a current state; "
        "'what was queued last Tuesday' is answered by the scan event log."),
    ("marine_vessel.py", "list_vessels"): (
        NOT_A_TIME_SERIES,
        "The vessel master (hulls, IMOs, call signs). `updated_at` is when we "
        "last touched the record, not when anything happened to the ship."),
    ("thread.py", "vessel_thread"): (
        NOT_A_TIME_SERIES,
        "Every container on ONE vessel call, from every source. The call is "
        "already the bound; a second date bound over it would silently drop "
        "containers that belong to the call being traced — the opposite of what "
        "an evidence screen is for."),
    ("thread.py", "thread_subjects"): (
        NOT_A_TIME_SERIES,
        "Computes which containers make good worked examples, by how many "
        "documents name them. Not a list of events."),

    # --- upstream proxies with no date parameter of their own ---------------
    ("gate_data.py", "captures"): (
        NO_UPSTREAM_FILTER,
        "Proxies the terminal gate-data service, which exposes no date "
        "parameter. A window here would filter the page already fetched."),
    ("gate_data.py", "reconciliations"): (
        NO_UPSTREAM_FILTER, "Same upstream, same absence of a date parameter."),
    ("gate_data.py", "customs_history"): (
        NO_UPSTREAM_FILTER, "Same upstream, same absence of a date parameter."),
    ("parking.py", "history"): (
        NO_UPSTREAM_FILTER,
        "Proxies the parking service. Its history call takes a vehicle, not a "
        "date range."),
    ("parking.py", "violations"): (
        NO_UPSTREAM_FILTER, "Same upstream."),

    # --- no timestamp anywhere in the backing table -------------------------
    ("rail.py", "rail_form11"): (
        NO_TIMESTAMP,
        "`core.form11_entry` records no time at all — the Form 11 workbooks "
        "carry a vessel visit and a destination, and no date column. Confirmed "
        "against jnpa_schema_v3."),
}


def excluded(router: str, func: str) -> Tuple[str, str] | None:
    """The (ground, reason) this endpoint is exempt, or None if it needs one."""
    return EXCLUSIONS.get((router, func))
