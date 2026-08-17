"""Container / vessel / vehicle lifecycle traversal — the golden thread.

WHAT THIS ANSWERS. "Show me this vessel, its containers, and the trucks that
carried them" — in one call, with every hop labelled by whether the corpus
actually evidences it.

WHY IT IS A BACKEND CONCERN. The hops live in nineteen tables with four different
spellings of the container key (`container_no` / `container_number`) and four of
the vehicle key (`vehicle_no` / `truck_no` / `plate` / `vehicle_number`). A
frontend assembling that itself would (a) need nineteen round trips and (b) have
no SQL to show, which the JNPA Notice §1(d) explicitly requires: *"the API queries
used to obtain the underlying data, so the working can be traced."* Every response
here therefore carries the statements it ran.

THE HONESTY RULE. A hop with no row is reported as `NOT_IN_CORPUS` with the table
that was searched — never omitted, and never interpolated from a neighbouring hop.
Measured on RDS on 17-Aug-2026: **42 of 11,957 containers reach a truck by any
route at all**, because the corpus's manifest set and its gate-document set share
no containers. A traversal that quietly dropped empty hops would make that
0.35 % look like 100 %.
"""
from .service import ContainerThreadService, Hop, ThreadResult

__all__ = ["ContainerThreadService", "Hop", "ThreadResult"]
