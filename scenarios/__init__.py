"""JNPA UC-III what-if scenarios (Sub-Criterion 5).

Each scenario is a module exposing::

    async def run(params: dict) -> ScenarioHandle
    async def reset(handle: ScenarioHandle) -> None

and is registered here in ``REGISTRY`` (the scenarios-runner discovers scenarios
through this map — the in-package equivalent of setuptools entry-points, kept
import-light so the runner has no plugin-discovery cost at startup). The
``[project.entry-points."jnpa.scenarios"]`` table in pyproject.toml mirrors this
for external discovery.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Dict, Protocol

from .handle import ScenarioHandle
from . import tfc1, tfc2, tfc3


class ScenarioModule(Protocol):
    NAME: str

    async def run(self, params: dict) -> ScenarioHandle: ...  # pragma: no cover
    async def reset(self, handle: ScenarioHandle) -> None: ...  # pragma: no cover


# name -> module. The runner calls module.run / module.reset.
REGISTRY: Dict[str, object] = {
    tfc1.NAME: tfc1,
    tfc2.NAME: tfc2,
    tfc3.NAME: tfc3,
}


def get_scenario(name: str):
    """Return the scenario module for ``name`` (case-insensitive), or None."""
    return REGISTRY.get(name.lower())


def scenario_names() -> list[str]:
    return list(REGISTRY.keys())


__all__ = ["REGISTRY", "ScenarioHandle", "get_scenario", "scenario_names"]
