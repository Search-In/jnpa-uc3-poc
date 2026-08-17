import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import RealtimeWorker from "@/workers/realtime.worker?worker";
import { api } from "@/lib/api";
import { getToken } from "@/lib/device";
import { appendAdvisories, loadAdvisories } from "@/lib/store";
import { alertToNotification, notifyDriver } from "@/lib/notify";
import { isForThisDriver } from "@/lib/addressing";
import type { Advisory, RerouteAdvisory, WsFrame } from "@/lib/types";

// One realtime hub for the whole app. It:
//   * runs the realtime worker (WebSocket to /api/ws), filtered to our device;
//   * listens for service-worker push messages (the backgrounded path);
//   * polls /api/trucks/{id}/route/latest as a fallback while the socket is down
//     so a re-route still lands within the 5 s SLA;
//   * persists every advisory to the 24 h IndexedDB cache (offline Inbox);
//   * surfaces the *pending* re-route so the Re-route screen can full-screen it.
//
// Consumers read `pendingReroute` (drives the confirmation screen), `advisories`
// (Inbox), and `status` (connection chip). `ackReroute` clears the pending one.

type Status = "connecting" | "open" | "closed";

interface RealtimeCtx {
  status: Status;
  advisories: Advisory[];
  pendingReroute: RerouteAdvisory | null;
  unread: number;
  pushHint: string | null;
  setPushHint: (s: string | null) => void;
  ackReroute: (state?: "ACK" | "DECLINE") => Promise<void>;
  markInboxRead: () => void;
  subscribe: (fn: (frame: WsFrame) => void) => () => void;
  /** D-12 — the most recent enforcement notice addressed to THIS driver, or null.
   *  Latest-wins: a driver needs the notice they just received, not a history. */
  violation: ViolationNotice | null;
  dismissViolation: () => void;
}

/** An enforcement notice as routers/violations.py emits it.
 *
 *  Every challan this system produces carries `badge: "SIMULATED"` and a
 *  disclosure, attached at the system of record rather than per screen. The
 *  notice MUST render them: a driver shown a fine with no indication it is a
 *  workflow demonstration would reasonably believe they had been fined. */
export interface ViolationNotice {
  case_id: string | number;
  plate?: string | null;
  vehicle?: string | null;
  driver?: string | null;
  violations?: Array<{ code?: string; label?: string; count?: number; fine?: number }>;
  fine?: number | null;
  challan_no?: string | null;
  issuance_mode?: string | null;
  badge?: string | null;
  is_legal_instrument?: boolean;
  authority_note?: string | null;
  disclosure?: string | null;
  assumption_ref?: string | null;
  status?: string | null;
  evidence_url?: string | null;
  ts?: string | null;
  device_id?: string | null;
  [k: string]: unknown;
}

const Ctx = createContext<RealtimeCtx | null>(null);

function wsUrl(deviceId: string): string {
  // Carry the DRIVER token as a query param: the WS handshake can't set an
  // Authorization header from the browser, so the gateway validates ?token=
  // when AUTH_ENABLED=true (and ignores it when auth is off).
  //
  // `device` binds the socket to this driver AT HANDSHAKE, so the gateway can
  // address advisories to it from the very first frame (with auth on, the
  // token's device_id claim wins and this is ignored). Without a binding the
  // gateway cannot tell a driver socket from a dashboard and would fall back to
  // sending it everything.
  const params = new URLSearchParams();
  const token = getToken();
  if (token) params.set("token", token);
  if (deviceId) params.set("device", deviceId);
  const q = params.toString() ? `?${params.toString()}` : "";
  // When VITE_GATEWAY_URL is set (statically-served build), point the socket at
  // the gateway directly; else same-origin (dev proxy / nginx /pwa).
  const base = (import.meta.env.VITE_GATEWAY_URL || "").replace(/\/$/, "");
  if (base) {
    const wsBase = base.replace(/^http/, "ws");
    return `${wsBase}/api/ws${q}`;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/ws${q}`;
}

function rerouteToAdvisory(r: RerouteAdvisory): Advisory {
  return {
    id: `reroute:${r.device_id}:${r.ts}`,
    type: "reroute",
    device_id: r.device_id,
    ts: r.ts,
    title: r.title || "Re-route advisory",
    body: r.body,
    reason: r.reason,
    gate_id: r.gate_id,
    dest: r.dest,
    route_km: r.route_km ?? null,
    requires_ack: r.requires_ack,
    severity: "warning",
  };
}

function alertToAdvisory(a: any): Advisory {
  const kind = a.kind || a.payload?.kind || "ALERT";
  const isChallan =
    String(kind).toUpperCase().includes("CHALLAN") ||
    a.severity === "REPORT_TO_POLICE" ||
    !!a.challan;
  return {
    id: `alert:${a.id || a.ts}`,
    type: isChallan ? "challan" : "alert",
    ts: a.ts || new Date().toISOString(),
    title: isChallan ? "Challan / enforcement notice" : `Alert — ${kind}`,
    body: a.payload?.message || a.payload?.detail || a.detail || undefined,
    severity: a.severity || "info",
    kind,
    gate_id: a.gate_id ?? null,
    plate: a.plate ?? null,
    payload: a.payload,
  };
}

export function RealtimeProvider({
  deviceId,
  plate,
  children,
}: {
  deviceId: string;
  plate?: string | null;
  children: React.ReactNode;
}) {
  const [status, setStatus] = useState<Status>("connecting");
  const [advisories, setAdvisories] = useState<Advisory[]>([]);
  const [pendingReroute, setPendingReroute] = useState<RerouteAdvisory | null>(null);
  const [pushHint, setPushHint] = useState<string | null>(null);
  const [violation, setViolation] = useState<ViolationNotice | null>(null);
  const [lastReadTs, setLastReadTs] = useState<number>(() =>
    Number(localStorage.getItem("jnpa.pwa.inboxReadTs") || 0),
  );

  const listeners = useRef<Set<(f: WsFrame) => void>>(new Set());
  const workerRef = useRef<Worker | null>(null);
  const lastRerouteTs = useRef<string | null>(null);

  const ingestReroute = useCallback(
    (r: RerouteAdvisory) => {
      if (!r || (r.device_id && r.device_id !== deviceId)) return;
      if (r.ts && r.ts === lastRerouteTs.current) return; // de-dupe across channels
      lastRerouteTs.current = r.ts ?? null;
      setPendingReroute(r);
      appendAdvisories([rerouteToAdvisory(r)]).then(setAdvisories);
    },
    [deviceId],
  );

  const handleFrame = useCallback(
    (frame: WsFrame) => {
      listeners.current.forEach((fn) => fn(frame));
      if (frame.type === "reroute") {
        ingestReroute(frame.payload as RerouteAdvisory);
      } else if (frame.type === "alert") {
        const a = frame.payload as any;
        // Only surface alerts this driver is actually addressed by. An alert
        // must POSITIVELY match — an unaddressed alert is NOT ours.
        if (isForThisDriver(a, deviceId, plate)) {
          appendAdvisories([alertToAdvisory(a)]).then(setAdvisories);
          // Raise an on-device notification / toast for this live alert. handleFrame
          // fires ONLY for live WS + push frames (never cache hydration), so this
          // won't replay history. Covers the congestion / parking / compliance /
          // emergency categories the backend detects but doesn't yet push.
          const kind = a.kind || a.payload?.kind || "ALERT";
          const body = a.payload?.message || a.payload?.detail || a.detail;
          notifyDriver(alertToNotification(String(kind), body));
        }
      } else if (frame.type === "violation_enforced") {
        // D-12. The gateway ADDRESSES this frame to the offending driver's
        // device when it can resolve one, but an unaddressed frame still fans
        // out to the control room — and would otherwise reach every connected
        // PWA carrying another driver's plate, fine and challan number. So the
        // same positive-match rule applies here as to alerts: absence of an
        // address is never evidence that a notice is ours.
        const v = frame.payload as unknown as ViolationNotice;
        if (isForThisDriver(v as never, deviceId, plate)) {
          setViolation(v);
          notifyDriver(
            alertToNotification(
              "VIOLATION",
              v.challan_no
                ? `Enforcement notice ${v.challan_no}`
                : `Enforcement notice for case ${v.case_id}`,
            ),
          );
        }
      }
    },
    [ingestReroute, deviceId, plate],
  );

  // The boot effect below re-runs only on deviceId, so read handleFrame through
  // a ref: the addressing filter it closes over depends on `plate`, which is
  // learned asynchronously (DriverSession). A stale closure would drop alerts
  // addressed to us by plate.
  const handleFrameRef = useRef(handleFrame);
  handleFrameRef.current = handleFrame;

  // --- boot: hydrate cache, start worker, wire SW push messages ---
  useEffect(() => {
    loadAdvisories().then(setAdvisories);

    const worker = new RealtimeWorker();
    workerRef.current = worker;
    worker.onmessage = (ev: MessageEvent) => {
      const m = ev.data || {};
      if (m.kind === "status") setStatus(m.status as Status);
      else if (m.kind === "frame") handleFrameRef.current(m.frame as WsFrame);
    };
    worker.postMessage({ cmd: "connect", url: wsUrl(deviceId), deviceId });

    const onSwMessage = (ev: MessageEvent) => {
      if (ev.data?.source === "push" && ev.data.frame) {
        const f = ev.data.frame;
        if (f.type === "reroute") ingestReroute(f as RerouteAdvisory);
        else handleFrameRef.current({ type: f.type, payload: f } as WsFrame);
      }
      // A notificationclick asked us to deep-link the focused window.
      if (ev.data?.navigate) location.hash = String(ev.data.navigate);
    };
    navigator.serviceWorker?.addEventListener?.("message", onSwMessage);

    return () => {
      worker.postMessage({ cmd: "close" });
      worker.terminate();
      navigator.serviceWorker?.removeEventListener?.("message", onSwMessage);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  // --- polling fallback while the socket is not open ---
  useEffect(() => {
    if (status === "open") return;
    let alive = true;
    const tick = async () => {
      try {
        const res = await api.latestReroute(deviceId);
        if (alive && res.advisory) ingestReroute(res.advisory as RerouteAdvisory);
      } catch {
        /* offline — rely on cache */
      }
    };
    tick();
    const t = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [status, deviceId, ingestReroute]);

  const ackReroute = useCallback(
    async (state: "ACK" | "DECLINE" = "ACK") => {
      try {
        await api.ackReroute(deviceId, state);
      } catch {
        /* best-effort; the banner still clears so the driver isn't stuck */
      }
      setPendingReroute(null);
    },
    [deviceId],
  );

  const markInboxRead = useCallback(() => {
    const now = Date.now();
    setLastReadTs(now);
    localStorage.setItem("jnpa.pwa.inboxReadTs", String(now));
  }, []);

  const subscribe = useCallback((fn: (f: WsFrame) => void) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  const unread = useMemo(
    () => advisories.filter((a) => Date.parse(a.ts) > lastReadTs).length,
    [advisories, lastReadTs],
  );

  const dismissViolation = useCallback(() => setViolation(null), []);

  const value: RealtimeCtx = {
    violation,
    dismissViolation,
    status,
    advisories,
    pendingReroute,
    unread,
    pushHint,
    setPushHint,
    ackReroute,
    markInboxRead,
    subscribe,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRealtime(): RealtimeCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useRealtime must be used within RealtimeProvider");
  return ctx;
}
