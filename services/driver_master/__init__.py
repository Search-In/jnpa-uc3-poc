"""Driver Master & Driver Intelligence service package (UC-III, additive).

Read-only surface over the licensed-port-driver registry (core.driver +
core.pdp) built in Phase-1. It NEVER writes to — and does not
import — the login-critical driver tables (core.driver_identity, driver_enrollments,
driver_faces, verification_logs, device_bindings); it only READS drivers /
driver_enrollments / verification_logs to derive enrollment + verification status
for display. Same router → service → repository shape as services.cargo.
"""
from .repository import DriverMasterRepository
from .service import DriverMasterService

__all__ = ["DriverMasterRepository", "DriverMasterService"]
