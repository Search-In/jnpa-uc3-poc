"""Hash-chain integrity: writer/verifier share one canonical function.

Unit layer (no DB): build a 3-link chain with ``chain_hash`` exactly as
``_audit`` writes it, then verify recomputation catches (a) a tampered detail,
(b) a forked prev_hash, (c) accepts the untampered chain. The live endpoint
(GET /api/violations/cases/{id}/verify-chain) is exercised in the demo
rehearsal against a TFC-2-generated case.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gateway.enforcement import chain_hash  # noqa: E402


def _mk_chain():
    rows = []
    prev = None
    for i, (event, frm, to) in enumerate([
        ("OPEN", None, "DETECTED"),
        ("REVIEW", "DETECTED", "REVIEWED"),
        ("CHALLAN", "REVIEWED", "CHALLAN_ISSUED"),
    ]):
        at = f"2026-07-31T12:00:0{i}+00:00"
        detail = {"n": i}
        h = chain_hash(prev, event=event, from_status=frm, to_status=to,
                       actor="test", detail=detail, at=at)
        rows.append({"event": event, "from_status": frm, "to_status": to,
                     "actor": "test", "detail": dict(detail), "at": at,
                     "prev_hash": prev, "hash": h})
        prev = h
    return rows


def _verify(rows) -> tuple[bool, int | None]:
    prev = None
    for i, r in enumerate(rows):
        expected = chain_hash(prev, event=r["event"], from_status=r["from_status"],
                              to_status=r["to_status"], actor=r["actor"],
                              detail=r["detail"], at=r["at"])
        if r["prev_hash"] != prev or r["hash"] != expected:
            return False, i
        prev = r["hash"]
    return True, None


def test_untampered_chain_verifies():
    ok, broken = _verify(_mk_chain())
    assert ok and broken is None


def test_tampered_detail_detected():
    rows = _mk_chain()
    rows[1]["detail"]["n"] = 999  # mutate a middle row's payload
    ok, broken = _verify(rows)
    assert not ok and broken == 1


def test_forked_prev_hash_detected():
    rows = _mk_chain()
    rows[2]["prev_hash"] = rows[0]["hash"]  # fork: link 3 claims link 1 as parent
    ok, broken = _verify(rows)
    assert not ok and broken == 2
