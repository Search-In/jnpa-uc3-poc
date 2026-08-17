"""The cargo vessel filter — the vessel->container hop of the golden thread.

`core.cargo.vessel_name` is written by several sources that disagree on case AND
on internal spacing for the same hull, so the comparison normalises both. An
exact match, or a `btrim` that only trims the ends, silently returns nothing and
reads as "this ship carried no cargo" — the failure this normalisation exists to
prevent. These cases pin the generated SQL; they need no database.
"""
import pytest

from services.cargo.repository import CargoRepository
from gateway.datewindow import DateWindow
from datetime import date


@pytest.fixture()
def repo():
    return CargoRepository.__new__(CargoRepository)


def test_no_filters_produces_no_where_clause(repo):
    """Adding these filters must not change an unfiltered call."""
    assert repo._where({}) == ("", {})


def test_vessel_name_normalises_case_and_all_whitespace(repo):
    clause, params = repo._where({"vessel_name": "  Xin  Hang Zhou "})
    # Both sides of the comparison get the same treatment, or the match is
    # asymmetric and only one spelling ever wins.
    assert clause.count("regexp_replace") == 2
    assert clause.count("upper(") == 2
    assert r"'\s+', ' ', 'g'" in clause
    assert params == {"vessel_name": "  Xin  Hang Zhou "}


def test_vessel_name_value_is_bound_never_interpolated(repo):
    clause, params = repo._where({"vessel_name": "O'BRIEN; DROP TABLE core.cargo"})
    assert "DROP" not in clause
    assert ":vessel_name" in clause
    assert params["vessel_name"] == "O'BRIEN; DROP TABLE core.cargo"


def test_exact_filters_are_unchanged(repo):
    clause, params = repo._where({"container_number": "DPWU9011100", "is_released": True})
    assert clause == ("WHERE container_number = :container_number "
                      "AND is_released = :is_released")
    assert "regexp_replace" not in clause


def test_vessel_and_window_compose(repo):
    clause, params = repo._where({
        "vessel_name": "XIN HANG ZHOU",
        "window": DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12)),
    })
    assert "regexp_replace" in clause
    assert "eta >= :dw_from" in clause and "eta < :dw_to_excl" in clause
    assert set(params) == {"vessel_name", "dw_from", "dw_to_excl"}


def test_date_col_is_whitelisted_not_interpolated(repo):
    """The column a window applies to is a caller choice, never raw client text."""
    with pytest.raises(ValueError):
        repo._where({"window": DateWindow(from_date=date(2026, 6, 6)),
                     "date_col": "eta); DROP TABLE core.cargo --"})


def test_open_window_adds_nothing(repo):
    assert repo._where({"window": DateWindow()}) == ("", {})


# --- vessel identity for the ships the corpus never names (GAP-VESSEL-01) ----
#
# The CHPOI03 IGM carries <IMOCodeofVessel> and <VesselCode> (the call sign) and
# NO name element. 10 of the 16 IMOs in the corpus are named nowhere in any
# supplied file, so 7,808 of 11,957 manifested containers could not be reached
# by ?vessel_name at all. These filters resolve through the IGM the cargo row
# already cites, so those boxes become addressable without a placeholder name
# ever being written into core.cargo.

def test_imo_filter_resolves_through_the_igm(repo):
    clause, params = repo._where({"imo_no": "9356294"})
    assert "core.igm" in clause
    assert "i.imo_no" in clause
    assert params["imo_no"] == "9356294"
    # correlated to the cargo row's own IGM, not a blanket join
    assert "source_igm_no" in clause


def test_call_sign_filter_resolves_through_the_igm(repo):
    clause, params = repo._where({"call_sign": "9HA5230"})
    assert "i.vessel_code" in clause
    assert params["call_sign"] == "9HA5230"


def test_vessel_identity_filters_bind_their_values(repo):
    """The value never reaches the SQL text — only the bound parameter."""
    hostile = "9356294'; DROP TABLE core.cargo; --"
    clause, params = repo._where({"imo_no": hostile})
    assert "DROP TABLE" not in clause
    assert params["imo_no"] == hostile


def test_absent_vessel_identity_adds_no_clause(repo):
    """A filter nobody asked for must not narrow the result set."""
    assert repo._where({}) == ("", {})
    clause, _ = repo._where({"vessel_name": "XIN HANG ZHOU"})
    assert "core.igm" not in clause


def test_name_and_imo_can_be_combined(repo):
    clause, params = repo._where({"vessel_name": "XIN HANG ZHOU", "imo_no": "9523017"})
    assert "vessel_name" in clause and "i.imo_no" in clause
    assert set(params) == {"vessel_name", "imo_no"}
