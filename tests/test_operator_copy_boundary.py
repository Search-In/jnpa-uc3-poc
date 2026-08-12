"""No internal/engineering wording may leave the API for an operator screen.

The Live Operations turnaround card renders the strings ``/api/kpi/dual-tat``
returns — ``method``, ``baseline_source``, ``render_rule.note`` and
``ground_truth_note`` — verbatim. Those strings used to carry the engineering
notes: the PoC baseline provenance, a docs/ path, which corpus legs have no
events, and the internal gap identifiers. A control-room screen is the wrong
place for any of it.

The sanitisation is done at the API boundary (gateway/routers/kpi.py), so this
test asserts on the response rather than on the component: a client that reads
the endpoint — this console, a future one, a report — cannot be handed the
internal wording by an upgraded gateway.

Nothing about the CALCULATION is asserted away here: targets, baselines and the
measured ground-truth markers are untouched and are covered by
tests/test_uc3_035_kpi_dashboard.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Iterator, List

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from gateway.routers import kpi as kpi_router  # noqa: E402

#: Wording that must never appear in an operator-facing payload. Matched
#: case-insensitively against every string the endpoint returns.
BANNED = (
    "poc demonstration baseline",
    "docs/assumptions.md",
    "assumptions.md",
    "corpus event",
    "corpus",
    "g6/g9",
    "gaps g6",
    "real measured turnarounds",
    "are simulated",
    "ui-122",
    "truck_out_ts",
    "truck_in_ts",
    "gate_document",
)


class _Cfg:
    # Unroutable on purpose: the marker query must degrade, and the copy under
    # test is returned either way.
    postgres_dsn = "postgresql+asyncpg://x:x@127.0.0.1:1/none"


class _State:
    cfg = _Cfg()


def _strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in the payload, with the key path that produced it."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


@pytest.fixture(scope="module")
def payload() -> dict:
    return asyncio.run(kpi_router.dual_tat(state=_State()))


#: Machine-metadata fields that are NOT display copy. `render_rule.ref` is the
#: internal spec id the payload carries for traceability; the console stopped
#: printing it (it used to be a chip on the card) and no screen may print it
#: again — asserted separately below.
NON_DISPLAY_PATHS = {"render_rule.ref"}


def test_dual_tat_payload_carries_no_internal_wording(payload):
    offenders: List[str] = []
    for path, value in _strings(payload):
        if path in NON_DISPLAY_PATHS:
            continue
        low = value.lower()
        for banned in BANNED:
            if banned in low:
                offenders.append(f"{path}: {banned!r} in {value!r}")
    assert offenders == [], "internal wording reached an operator payload:\n" + "\n".join(offenders)


def test_both_arms_still_describe_themselves_to_the_operator(payload):
    """Sanitised, not stripped: the card still explains what it is showing."""
    for arm in ("terminal", "driver"):
        a = payload["pair"][arm]
        assert a["method"] and len(a["method"]) > 20
        assert a["baseline_source"]
        assert a["label"] and a["unit"] == "min"


def test_the_measurement_is_untouched(payload):
    """Copy changed; the numbers did not."""
    from jnpa_shared.kpi import KPI_TARGETS

    target = KPI_TARGETS["tat_inside_port"]
    for arm in ("terminal", "driver"):
        assert payload["pair"][arm]["target"] == target.target
        assert payload["pair"][arm]["baseline"] == target.baseline
    # The pair rule itself is unchanged — still one payload, still both arms.
    assert payload["render_rule"]["must_render_together"] is True
    assert payload["render_rule"]["ref"] == "UI-122"   # kept as machine metadata…
    assert "UI-122" not in payload["render_rule"]["note"]  # …never as screen copy


def test_the_ground_truth_note_is_operator_wording_in_both_branches():
    """Both branches of the marker note are sanitised, not just the empty one."""
    for note in (kpi_router.OPERATOR_GROUND_TRUTH_NOTE,
                 kpi_router.OPERATOR_GROUND_TRUTH_EMPTY_NOTE):
        low = note.lower()
        assert not any(b in low for b in BANNED), note
    # The non-empty branch still states the rule that matters operationally:
    # markers are references, never folded into the headline average.
    assert "never averaged" in kpi_router.OPERATOR_GROUND_TRUTH_NOTE.lower()
