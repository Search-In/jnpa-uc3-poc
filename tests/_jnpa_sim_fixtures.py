"""Shared fixtures for the JNPA Port-Data API contract tests.

Builds a small deterministic corpus in the sim's expected folder layout and
wires the real client to the real sim in-process (httpx.ASGITransport) — no
network, no DB. The fixture corpus is intentionally tiny (fast tests) but
shaped to exercise the hazards: >= 6 customs files so the sim's boundary-tie
fixture (4 records sharing one publishedAt) activates.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "ingest",):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from jnpa_portdata_sim import app as sim_app_module  # noqa: E402
from jnpa_portdata_sim.app import SimState, derive_client_key  # noqa: E402
from jnpa_portdata_sim.config import SimConfig  # noqa: E402

SIM_EMAIL = "sim@keltron.test"
SIM_KEY = derive_client_key(SIM_EMAIL)


def build_fixture_corpus(tmp_path: Path) -> Path:
    """A miniature sample-data-pack Data/ dir in the sim's folder layout."""
    data = tmp_path / "Data"
    customs = data / "5- Customs" / "IGM"
    customs.mkdir(parents=True)
    for i in range(8):  # >= 6 => the sim ties the last 4 timestamps
        (customs / f"CHPOI03_{1000 + i}_06-06-2026.xml").write_bytes(
            f"<CHPOI03Payload><IGM_NO>{1000 + i}</IGM_NO></CHPOI03Payload>"
            .encode())
    ooc = data / "5- Customs" / "OOC"
    ooc.mkdir(parents=True)
    (ooc / "CHPOI10_9001_06-06-2026.xml").write_bytes(
        b"<CHPOI10Payload><BE_NO>9001</BE_NO></CHPOI10Payload>")
    marine = data / "1-NLP Marine" / "VESPRO"
    marine.mkdir(parents=True)
    for i in range(3):
        (marine / f"VESPRO_20260{i}.xml").write_bytes(
            f"<VESPRO><IMO>91234{i}</IMO></VESPRO>".encode())
    cfs = data / "13-CFS-ECY"
    cfs.mkdir(parents=True)
    (cfs / "CFS-CODECO.xlsx").write_bytes(b"PK\x03\x04 fake xlsx bytes")
    transport = data / "11-Transport Data"
    transport.mkdir(parents=True)
    (transport / "TransporterDetails.xlsx").write_bytes(b"PK\x03\x04 fake")
    # report + static folders exist but are never indexed
    (data / "7-Berthing Reports").mkdir(parents=True)
    (data / "12-Performance & Daily Reports").mkdir(parents=True)
    (data / "2-JNPA_Sea_Channels_Bathymetry").mkdir(parents=True)
    return data


def fresh_sim(data_dir: Path, **config_overrides) -> "SimState":
    """(Re)build the sim's module state around a fixture corpus and return
    it. Handlers resolve the module-global ``state`` at call time, so
    swapping it re-seeds the whole app in-process."""
    defaults = dict(
        data_dir=str(data_dir),
        client_emails=[SIM_EMAIL],
        bad_key_delay_s=0.01,      # keep the deliberate slow-lane symbolic
    )
    defaults.update(config_overrides)
    state = SimState(SimConfig(**defaults))
    sim_app_module.state = state
    return state


def sim_asgi_app():
    return sim_app_module.app
