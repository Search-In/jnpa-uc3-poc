"""The golden-thread traversal.

Two properties matter more than any individual hop, and both were broken during
development in ways that produced a *plausible* answer rather than an error:

  1. A failed query must never be reported as "the corpus has nothing here".
     A single bad column name aborted the transaction and every later hop came
     back empty — the traversal cheerfully reported 17 of 18 steps as
     NOT_IN_CORPUS when it had simply stopped being able to look.
  2. The traversal is read-only. It runs against a database five engineers share.
"""
import pytest

from services.thread.service import (IMPORT, EXPORT, SHARED, _HOPS,
                                     ContainerThreadService, ThreadWriteAttempt)


@pytest.fixture()
def svc():
    return ContainerThreadService(dsn=None)


# --------------------------------------------------------------- read-only --
@pytest.mark.parametrize("sql", [
    "insert into core.cargo values (1)",
    "UPDATE core.cargo SET vessel_name='x'",
    "delete from core.gate_document",
    "drop table core.eir",
    "truncate core.cargo",
    # The classic bypass: a CTE that writes and returns rows.
    "WITH x AS (DELETE FROM core.cargo RETURNING *) SELECT * FROM x",
])
def test_write_statements_are_refused(svc, sql):
    with pytest.raises(ThreadWriteAttempt):
        svc.assert_read_only(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select container_no from core.igm_line_container where container_no = :cn",
    "WITH t AS (SELECT 1) SELECT * FROM t",
])
def test_reads_are_allowed(svc, sql):
    svc.assert_read_only(sql)  # must not raise


# ------------------------------------------------------------- hop integrity --
def test_every_hop_declares_a_real_stage():
    assert {h.stage for h in _HOPS} <= {IMPORT, EXPORT, SHARED}


def test_hop_keys_are_unique():
    keys = [h.key for h in _HOPS]
    assert len(keys) == len(set(keys))


def test_both_lifecycle_directions_are_covered():
    """The brief is import AND export; a traversal missing one is half a twin."""
    stages = {h.stage for h in _HOPS}
    assert IMPORT in stages and EXPORT in stages


def test_the_truck_bearing_hops_are_declared():
    """The container->truck link is the whole point; these are the sources that
    can carry it, and the vehicle column differs in every one of them."""
    veh = {h.key: h.vehicle_col for h in _HOPS if h.vehicle_col}
    assert veh["codeco"] == "vehicle_no"
    assert veh["eir"] == "truck_no"
    assert veh["gate_event"] == "plate"
    assert veh["cargo"] == "vehicle_number"
    assert veh["gate_document"] == "vehicle_no"


def test_every_hop_names_its_corpus_source():
    """A hop with no stated source cannot appear in an evidence trail."""
    for h in _HOPS:
        assert h.source, f"hop {h.key} does not name the corpus family behind it"


def test_container_key_column_is_one_of_the_two_real_spellings():
    for h in _HOPS:
        assert h.container_col in ("container_no", "container_number"), h.key


# ------------------------------------------------- synthetic must be visible --
# A fabricated hop that renders like corpus evidence is the single most damaging
# thing this system could show an evaluator, and JNPA's 31-Jul notice asks for
# defects to be REPORTED rather than papered over. These pin the machinery that
# keeps a fixture visibly a fixture.

def test_every_hop_declares_where_its_provenance_lives():
    """A hop with no provenance column cannot report a row as synthetic."""
    for h in _HOPS:
        assert h.provenance_col, f"hop {h.key} declares no provenance column"


def test_synthetic_detection_covers_the_markers_actually_written():
    """`seed_synthetic_flow.py` writes these; `is_synthetic` must catch each."""
    from services.thread.service import Hop
    for marker in ("SYNTHETIC:flow-v1", "SYNTHETIC-FLOW", "SIM", "sim"):
        h = Hop(key="k", label="l", stage=SHARED, verdict="FOUND",
                source_table="t", source_files="f", provenance=[marker])
        assert h.is_synthetic, f"{marker!r} not recognised as synthetic"


def test_real_provenance_is_not_flagged_synthetic():
    """The corpus's own values must never be mistaken for fixtures."""
    from services.thread.service import Hop
    for marker in ("REAL", "MANUAL", "API", "CORPUS-IGM"):
        h = Hop(key="k", label="l", stage=SHARED, verdict="FOUND",
                source_table="t", source_files="f", provenance=[marker])
        assert not h.is_synthetic, f"{marker!r} wrongly flagged synthetic"


def test_provenance_is_exposed_at_the_top_of_the_hop():
    """It must be readable without digging through row dicts."""
    from services.thread.service import Hop
    d = Hop(key="k", label="l", stage=SHARED, verdict="FOUND", source_table="t",
            source_files="f", provenance=["SYNTHETIC:flow-v1"]).as_dict()
    assert d["synthetic"] is True
    assert d["provenance"] == ["SYNTHETIC:flow-v1"]
