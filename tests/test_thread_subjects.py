"""GAP-UI-06 — worked-example containers must be computed, not listed.

UC-2's import board hardcoded five container numbers. They were correct when
written; the problem is that a literal list is a claim about the DATABASE frozen
into the frontend, and after an ingest it can name a box whose chain no longer
resolves while staying silent about a better one.

Two properties matter and neither is obvious:

  * ranking must count JNPA DOCUMENTS, not sources. `core.gate_event`,
    `core.container_job_assignment` and `core.cargo_movement_event` are
    simulator-fed and name tens of thousands of containers the corpus says
    nothing about; ranked by raw source count they bury every document-evidenced
    chain (measured: the top 6 were all simulator rows).
  * selection must be a set COVER, not a top-N. Ranked purely by coverage the
    answer was five containers all demonstrating the same three hops, which
    teaches a viewer nothing about the other fifteen steps.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.thread.service import _HOPS  # noqa: E402


def test_corpus_hops_are_distinguished_from_simulator_hops():
    corpus = {h.key for h in _HOPS if h.is_corpus}
    simulated = {h.key for h in _HOPS if not h.is_corpus}

    # The document-backed sources: every one names a numbered corpus group.
    assert "manifest" in corpus and "gate_document" in corpus
    assert "eir" in corpus and "codeco" in corpus

    # The ones fed by our own telemetry or derived, which must never be counted
    # as evidence that JNPA documented something.
    assert {"gate_event", "job", "yard_move", "cargo"} <= simulated
    assert not (corpus & simulated)


def test_every_hop_declares_which_side_it_is_on():
    """A hop with no source string would silently count as simulated."""
    for h in _HOPS:
        assert h.source, f"{h.key} declares no source family"


def test_corpus_hops_name_a_numbered_corpus_group():
    for h in _HOPS:
        if h.is_corpus:
            assert h.source[0].isdigit(), h.source
        else:
            assert not h.source[0].isdigit(), h.source
