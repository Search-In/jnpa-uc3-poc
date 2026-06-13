"""Traffic congestion forecaster for the JNPA UC-III Digital Twin PoC.

A GraphSAGE encoder over the NH-348 corridor graph feeds a 2-layer LSTM that
predicts ``P(congested in next 15 min)`` per segment from a rolling window of
60-second aggregates. See ``model.py`` (network), ``graph.py`` (PyG graph from
``jnpa_shared.corridor``), ``features.py`` (rolling feature windows),
``train.py`` (training + held-out metrics) and ``infer.py`` (FastAPI on 8311).
"""

__version__ = "0.1.0"
