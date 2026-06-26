import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "@/lib/api";

// DriverSession — the single, global source of truth for "who is this driver".
//
// The PoC pairs a device deterministically (6-digit code -> TRK-id) and there is
// no pairing server that hands back a full driver profile. So at login we ASSEMBLE
// the driver context once from the endpoints that do exist:
//
//   * vehicle  <- the live truck snapshot  (GET /api/trucks/{id} -> record.plate)
//   * identity <- the driver's own enrolment record, if this device has enrolled
//                 (GET /api/identity/enrol-request/{driver_id})
//
// The assembled context is persisted under one key and reused everywhere (Home,
// Enrol, …). It is loaded ONCE per device and never refetched unless the caller
// explicitly refresh()es (e.g. straight after submitting an enrolment) or the
// device is unpaired. This is the contract the product spec asks for: one driver
// session, one global state, no duplicate fetches.

// The driver_id a completed enrolment is stored under (Enrol writes this on
// submit). Shared here so the session can recover the driver identity after a
// refresh without re-entering it.
export const ENROL_DRIVER_KEY = "jnpa_enrol_driver_id";
const SESSION_KEY = "jnpa.pwa.session";

export type DriverStatus = "ACTIVE" | "PENDING" | "REJECTED" | "REENROLL" | "UNVERIFIED";

export interface DriverContext {
  deviceId: string;
  driverId: string | null;
  name: string | null;
  vehicle: string | null;
  status: DriverStatus;
  // ISO timestamp of when this context was last assembled from the backend.
  loadedAt: string;
}

interface SessionApi {
  session: DriverContext;
  loading: boolean;
  /** Re-assemble the context from the backend (use sparingly — e.g. post-enrolment). */
  refresh: () => Promise<void>;
  /** Merge in fields known locally (e.g. the just-submitted enrolment) without a fetch. */
  applyEnrolment: (patch: Partial<DriverContext>) => void;
}

const Ctx = createContext<SessionApi | null>(null);

function normaliseStatus(s: string | null | undefined): DriverStatus {
  const v = (s || "").toUpperCase();
  if (v === "ACTIVE" || v === "PENDING" || v === "REJECTED" || v === "REENROLL") return v;
  return "UNVERIFIED";
}

function readPersisted(deviceId: string): DriverContext | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as DriverContext;
    // Only honour a cached session that belongs to the currently paired device.
    if (parsed && parsed.deviceId === deviceId) return parsed;
  } catch {
    /* corrupt / unavailable — fall through to a fresh assemble */
  }
  return null;
}

function persist(session: DriverContext): void {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    /* storage unavailable (private mode) — session simply lives in memory */
  }
}

function readEnrolDriverId(): string | null {
  try {
    const v = localStorage.getItem(ENROL_DRIVER_KEY);
    return v && v.trim() ? v.trim() : null;
  } catch {
    return null;
  }
}

// Assemble the driver context from whatever the backend can tell us about this
// device. Every lookup is best-effort: a fresh, never-enrolled device still
// yields a usable context (status UNVERIFIED) so Home can prompt enrolment.
async function assemble(deviceId: string, plate?: string | null): Promise<DriverContext> {
  let vehicle = plate ?? null;
  let driverId = readEnrolDriverId();
  let name: string | null = null;
  let status: DriverStatus = "UNVERIFIED";

  // Vehicle from the live truck snapshot (the plate the device is bound to).
  if (!vehicle) {
    try {
      const env = await api.truck(deviceId);
      vehicle = env.record.plate ?? null;
    } catch {
      /* device not yet known to the gateway */
    }
  }

  // Identity from the driver's own enrolment record, if this device enrolled.
  if (driverId) {
    try {
      const rec = await api.enrolStatus(driverId);
      driverId = rec.driver_id || driverId;
      name = (rec as any).name ?? null;
      vehicle = (rec as any).vehicle_no || vehicle;
      status = normaliseStatus(rec.status);
    } catch {
      /* enrolment record gone (e.g. purged) — keep UNVERIFIED */
    }
  }

  return {
    deviceId,
    driverId,
    name,
    vehicle,
    status,
    loadedAt: new Date().toISOString(),
  };
}

export function DriverSessionProvider({
  deviceId,
  plate,
  children,
}: {
  deviceId: string;
  plate?: string | null;
  children: React.ReactNode;
}) {
  // Hydrate synchronously from the persisted session so a remount never flickers
  // and never refetches. A device with no cached session starts as a minimal
  // placeholder and loads once below.
  const [session, setSession] = useState<DriverContext>(
    () =>
      readPersisted(deviceId) ?? {
        deviceId,
        driverId: null,
        name: null,
        vehicle: plate ?? null,
        status: "UNVERIFIED",
        loadedAt: new Date().toISOString(),
      },
  );
  const [loading, setLoading] = useState(() => readPersisted(deviceId) === null);
  const loadedRef = useRef(readPersisted(deviceId) !== null);

  const commit = useCallback((next: DriverContext) => {
    setSession(next);
    persist(next);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    const next = await assemble(deviceId, plate);
    commit(next);
    loadedRef.current = true;
    setLoading(false);
  }, [deviceId, plate, commit]);

  const applyEnrolment = useCallback((patch: Partial<DriverContext>) => {
    setSession((prev) => {
      const next = { ...prev, ...patch, deviceId: prev.deviceId };
      persist(next);
      return next;
    });
  }, []);

  // Load exactly once per device — only when nothing was hydrated from storage.
  useEffect(() => {
    if (loadedRef.current) return;
    let alive = true;
    (async () => {
      const next = await assemble(deviceId, plate);
      if (!alive) return;
      commit(next);
      loadedRef.current = true;
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  const value = useMemo<SessionApi>(
    () => ({ session, loading, refresh, applyEnrolment }),
    [session, loading, refresh, applyEnrolment],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDriverSession(): SessionApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useDriverSession must be used within a DriverSessionProvider");
  return v;
}
