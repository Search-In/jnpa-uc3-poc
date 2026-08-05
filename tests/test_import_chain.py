"""Import lifecycle chain: IGM -> Cargo -> ... -> Release.

The audit's largest data gap was between the first two links: core.igm_line_container
held 12,235 real manifested containers while core.cargo held 19 rows, so
/api/customs/reconcile — which only ever UPDATES containers already in cargo —
could never bind more than those 19, and no manifested box could be discharged,
yard-assigned, verified or released.

These tests pin the materialiser that closes it, and the full state machine it
feeds, using a fake repository (no DB) as tests/test_cargo.py does.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routers import customs as CR
from services.customs import CustomsService


class FakeCustomsRepo:
    """Models the two SQL passes the bridge relies on.

    ``manifest`` is core.igm_line_container; ``cargo`` is core.cargo. The
    materialiser only inserts containers missing from cargo (ON CONFLICT DO
    NOTHING) and only ISO-6346-shaped ones — both reproduced here.
    """

    def __init__(self, manifest: list[tuple[str, str]], cargo: Optional[dict] = None) -> None:
        self.manifest = manifest                      # [(container_no, igm_no)]
        self.cargo: dict[str, dict] = dict(cargo or {})
        self.ooc: set[str] = set()
        self.rms: set[str] = set()
        self.events: list[tuple[str, str]] = []
        self.notifications: list[str] = []

    @staticmethod
    def _iso_shaped(cn: str) -> bool:
        return (len(cn) == 11 and cn[:4].isalpha() and cn[:4].isupper()
                and cn[4:].isdigit())

    async def materialize_cargo_from_igm(self, *, igm_no=None, limit=5000) -> dict:
        rows = [(cn, igm) for cn, igm in self.manifest
                if self._iso_shaped(cn) and (igm_no is None or igm == igm_no)]
        seen, candidates = set(), []
        for cn, igm in rows[:limit]:
            if cn in seen:
                continue
            seen.add(cn)
            candidates.append((cn, igm))
        created = []
        for cn, igm in candidates:
            if cn in self.cargo:
                continue                              # ON CONFLICT DO NOTHING
            self.cargo[cn] = {"container_number": cn, "customs_status": "PENDING",
                              "is_released": False, "lifecycle_status": "CREATED",
                              "direction": "IMPORT", "source_igm_no": igm}
            created.append(cn)
        return {"candidates": len(candidates), "created": len(created),
                "skipped_existing": len(candidates) - len(created),
                "sample": created[:50]}

    async def reconcile_cargo_status(self) -> dict:
        cleared, inspect = [], []
        for cn, row in self.cargo.items():
            if cn in self.ooc and row["customs_status"] != "CLEARED":
                row["customs_status"] = "CLEARED"
                cleared.append(cn)
            elif (cn in self.rms and cn not in self.ooc
                  and row["customs_status"] not in ("CLEARED", "UNDER_INSPECTION")):
                row["customs_status"] = "UNDER_INSPECTION"
                inspect.append(cn)
        return {"cleared": cleared, "under_inspection": inspect}

    async def record_event(self, event, *, module, container_no, reference, payload):
        self.events.append((event, container_no))

    async def create_cargo_notification(self, container_number, *, notification_type,
                                        severity, message):
        self.notifications.append(container_number)


@pytest.fixture
def manifest() -> list[tuple[str, str]]:
    # Real corpus values + the manifest noise the audit documented.
    return [
        ("DPWU9011100", "1194313"),
        ("CSNU1399404", "1193612"),
        ("DFSU1691030", "1194379"),
        ("DFSU1687214", "1194379"),
        ("UETU7497400", "1196874"),
        ("MEDU3056840", "1196874"),
        ("DPWU9011100", "1194313"),      # duplicate manifest line
        ("POWERPACK1", "1196874"),       # pseudo-container (CTO noise)
        ("", "1196874"),                 # blank cell
        ("MRKU95276", "1194257"),        # too short
    ]


@pytest.fixture
def repo(manifest) -> FakeCustomsRepo:
    return FakeCustomsRepo(manifest)


@pytest.fixture
def svc(repo) -> CustomsService:
    return CustomsService(repository=repo)


# --------------------------------------------------------------- materialise
@pytest.mark.asyncio
async def test_igm_containers_become_cargo_rows(svc, repo):
    """The missing IGM -> Cargo link: every manifested box gets a cargo row."""
    out = await svc.materialize_cargo(reconcile=False)

    assert out["created"] == 6                 # 6 distinct valid containers
    assert set(repo.cargo) == {"DPWU9011100", "CSNU1399404", "DFSU1691030",
                               "DFSU1687214", "UETU7497400", "MEDU3056840"}
    # Nothing is fast-forwarded: the state machine still has to be walked.
    assert all(r["lifecycle_status"] == "CREATED" for r in repo.cargo.values())
    assert all(r["customs_status"] == "PENDING" for r in repo.cargo.values())
    # Provenance is recorded (0115).
    assert repo.cargo["UETU7497400"]["source_igm_no"] == "1196874"
    assert repo.cargo["DPWU9011100"]["direction"] == "IMPORT"


@pytest.mark.asyncio
async def test_manifest_noise_never_enters_the_lifecycle(svc, repo):
    """Blank cells, pseudo-containers and short codes are not containers."""
    await svc.materialize_cargo(reconcile=False)
    assert "POWERPACK1" not in repo.cargo
    assert "MRKU95276" not in repo.cargo
    assert "" not in repo.cargo


@pytest.mark.asyncio
async def test_materialise_is_idempotent(svc, repo):
    """Running it twice creates nothing — safe to wire into an import trigger."""
    first = await svc.materialize_cargo(reconcile=False)
    second = await svc.materialize_cargo(reconcile=False)
    assert first["created"] == 6
    assert second["created"] == 0
    assert second["skipped_existing"] == 6
    assert len(repo.cargo) == 6


@pytest.mark.asyncio
async def test_materialise_never_disturbs_a_box_already_moving(svc, repo):
    """A container mid-yard keeps its state — the bridge only fills the gap."""
    repo.cargo["DPWU9011100"] = {"container_number": "DPWU9011100",
                                 "customs_status": "CLEARED", "is_released": False,
                                 "lifecycle_status": "YARD_ASSIGNED",
                                 "direction": "IMPORT", "source_igm_no": "1194313"}
    await svc.materialize_cargo(reconcile=False)
    assert repo.cargo["DPWU9011100"]["lifecycle_status"] == "YARD_ASSIGNED"
    assert repo.cargo["DPWU9011100"]["customs_status"] == "CLEARED"


@pytest.mark.asyncio
async def test_materialise_can_be_scoped_to_one_igm(svc, repo):
    out = await svc.materialize_cargo(igm_no="1194379", reconcile=False)
    assert out["created"] == 2
    assert set(repo.cargo) == {"DFSU1691030", "DFSU1687214"}


# ------------------------------------------------------ materialise + bind
@pytest.mark.asyncio
async def test_materialise_then_reconcile_binds_customs_status(svc, repo):
    """One call takes a freshly imported IGM all the way to correct customs_status.

    This is the whole point: before the bridge existed, reconcile had 19 rows to
    work with no matter how many manifests had been imported.
    """
    repo.ooc.add("CSNU1399404")       # Out-Of-Charge issued
    repo.rms.add("UETU7497400")       # RMS-selected for scanning

    out = await svc.materialize_cargo()          # reconcile=True by default

    assert out["created"] == 6
    assert out["reconciled"]["cleared"] == 1
    assert out["reconciled"]["under_inspection"] == 1
    assert repo.cargo["CSNU1399404"]["customs_status"] == "CLEARED"
    assert repo.cargo["UETU7497400"]["customs_status"] == "UNDER_INSPECTION"
    # The scan hold raised a notification on the EXISTING cargo feed.
    assert "UETU7497400" in repo.notifications


# -------------------------------------------------------------- HTTP layer
def test_materialize_endpoint(svc):
    app = FastAPI()
    app.include_router(CR.router)
    app.dependency_overrides[CR.get_service] = lambda: svc
    client = TestClient(app)

    res = client.post("/api/customs/materialize?reconcile_after=false")
    assert res.status_code == 200
    body = res.json()
    assert body["created"] == 6
    assert body["candidates"] == 6

    # Idempotent over HTTP too.
    again = client.post("/api/customs/materialize?reconcile_after=false")
    assert again.json()["created"] == 0


def test_materialize_endpoint_scoped_to_one_igm(svc):
    app = FastAPI()
    app.include_router(CR.router)
    app.dependency_overrides[CR.get_service] = lambda: svc
    client = TestClient(app)

    res = client.post("/api/customs/materialize?igm_no=1196874&reconcile_after=false")
    assert res.status_code == 200
    assert res.json()["created"] == 2       # UETU7497400 + MEDU3056840


# ------------------------------------------- the chain the materialiser feeds
@pytest.mark.asyncio
async def test_full_import_chain_created_to_released():
    """CREATED -> DISCHARGED -> YARD -> POSITION -> VERIFIED -> RELEASED.

    Runs against the real cargo state machine (services.cargo) so the states the
    materialiser hands over are the ones the lifecycle actually accepts.
    """
    from services.cargo.service import (LC_CREATED, LC_RELEASED, LC_VERIFIED,
                                        LC_VESSEL_DISCHARGED, LC_YARD_ASSIGNED,
                                        LC_YARD_POSITION_ALLOCATED, can_transition)

    # A materialised row starts here, and the first legal move is discharge.
    assert can_transition(LC_CREATED, LC_VESSEL_DISCHARGED)
    assert can_transition(LC_VESSEL_DISCHARGED, LC_YARD_ASSIGNED)
    assert can_transition(LC_YARD_ASSIGNED, LC_YARD_POSITION_ALLOCATED)
    assert can_transition(LC_YARD_POSITION_ALLOCATED, LC_VERIFIED)
    assert can_transition(LC_VERIFIED, LC_RELEASED)

    # Mandatory gates cannot be skipped: no release without verification, and no
    # yard assignment before the box is off the vessel.
    assert not can_transition(LC_YARD_ASSIGNED, LC_RELEASED)
    assert not can_transition(LC_CREATED, LC_YARD_ASSIGNED)
    assert not can_transition(LC_CREATED, LC_RELEASED)
    # And it is forward-only.
    assert not can_transition(LC_RELEASED, LC_VERIFIED)


def test_export_states_are_reachable_on_the_same_column():
    """0115 widened the cargo CHECK; the import states must all still be legal."""
    import re
    sql = open("infra/postgres/v3/0115_lifecycle_completion.sql").read()
    check = re.search(r"cargo_lifecycle_status_check\s+CHECK \(lifecycle_status IN \((.*?)\)\)",
                      sql, re.S)
    assert check, "the widened CHECK constraint must be present in 0115"
    values = set(re.findall(r"'([A-Z_0-9]+)'", check.group(1)))
    # every pre-existing import state survives the widening
    assert {"CREATED", "VESSEL_DISCHARGED", "YARD_ASSIGNED", "YARD_POSITION_ALLOCATED",
            "REEFER_PLANNED", "RAKE_ASSIGNED", "SCAN_PENDING", "VERIFIED",
            "RELEASED"} <= values
    # and the export leg is now representable
    assert {"EXPORT_BOOKED", "FORM13_ISSUED", "EXPORT_GATE_IN", "VGM_CAPTURED",
            "LEO_GRANTED", "LOAD_LISTED", "VESSEL_LOADED"} <= values
