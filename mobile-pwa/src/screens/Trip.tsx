import { lazy, Suspense, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CorridorGeometry, Gate, TasSlot, TruckEnvelope } from "@/lib/types";
import { Card, Chip, Spinner, Stat } from "@/components/ui";
import { fmtClock, fmtEta, fmtKm, fmtSpeed, gateShort } from "@/lib/format";
import { useRealtime } from "@/hooks/RealtimeContext";

// maplibre-gl is ~700 KB; lazy-load it so it never blocks first paint (FCP
// target on Fast 3G). The map slots in once the chunk lands.
const MiniMap = lazy(() => import("@/components/MiniMap"));

// Trip — the driver's home screen: current target gate, ETA, speed, traffic
// ahead (mini-map), and the "Slot at Gate" widget showing the next allocated
// TAS window. Polls the truck envelope every 4 s; the realtime worker updates
// the live position between polls.

export default function Trip({ deviceId }: { deviceId: string }) {
  const { status, subscribe } = useRealtime();
  const [truck, setTruck] = useState<TruckEnvelope | null>(null);
  const [corridor, setCorridor] = useState<CorridorGeometry | undefined>();
  const [gates, setGates] = useState<Gate[] | undefined>();
  const [slot, setSlot] = useState<TasSlot | null>(null);
  const [livePos, setLivePos] = useState<{ lat: number; lon: number } | null>(null);
  const [parking, setParking] = useState<{ available?: number; capacity?: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Static geometry — fetched once.
  useEffect(() => {
    api
      .corridor()
      .then(setCorridor)
      .catch(() => undefined);
    api
      .gates()
      .then((r) => setGates(r.gates))
      .catch(() => undefined);
    // Parking availability inside the geo-fenced port (SCOPE-R1 / IU2).
    api
      .parkingSummary()
      .then((s) => setParking({ available: s.total_available, capacity: s.total_capacity }))
      .catch(() => undefined);
  }, []);

  // Truck envelope poll.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const env = await api.truck(deviceId);
        if (!alive) return;
        setTruck(env);
        setErr(null);
        const gate = env.record.gate_id;
        if (gate) {
          api
            .tasSlots(gate)
            .then((r) => {
              const next = r.slots
                .filter((s) => s.status !== "CANCELLED")
                .sort((a, b) => Date.parse(a.start) - Date.parse(b.start))[0];
              if (alive) setSlot(next ?? null);
            })
            .catch(() => undefined);
        }
      } catch (e: any) {
        if (alive) setErr(e?.status === 404 ? "Awaiting first GPS fix…" : "Position unavailable");
      }
    };
    tick();
    const t = setInterval(tick, 4000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [deviceId]);

  // Live position from the realtime feed (between polls).
  useEffect(() => {
    return subscribe((frame) => {
      if (frame.type === "truck_position" && frame.payload?.device_id === deviceId) {
        setLivePos({ lat: frame.payload.lat, lon: frame.payload.lon });
      }
    });
  }, [subscribe, deviceId]);

  const rec = truck?.record;
  const pos = livePos ?? rec?.position ?? null;
  const slotResched = slot?.status === "RESCHEDULED";

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
        <Chip status={status === "open" ? "ok" : status === "connecting" ? "warn" : "down"}>
          {status === "open" ? "Live" : status === "connecting" ? "Connecting" : "Offline"}
        </Chip>
        {truck?.elevated_scrutiny ? (
          <Chip status="warn">Elevated scrutiny +{truck.gate_boom_delay_s}s</Chip>
        ) : (
          <Chip status="ok">{truck?.decision_path ?? "—"}</Chip>
        )}
      </div>

      {/* Slot at Gate widget */}
      <div className={`slot ${slotResched ? "resched" : ""}`}>
        <div className="lbl">{slotResched ? "Slot rescheduled" : "Slot at Gate"}</div>
        <div className="time">{slot ? fmtClock(slot.start) : "—"}</div>
        <div className="gate">
          {slot ? (
            <>
              {slot.slot_id} · Gate {gateShort(slot.gate_id)}
              {slotResched && slot.rescheduled_to ? ` → ${gateShort(slot.rescheduled_to)}` : ""}
            </>
          ) : (
            "No allocated window yet"
          )}
        </div>
      </div>

      {/* ETA / speed / remaining */}
      <div className="stat-row" style={{ marginBottom: 12 }}>
        <Stat value={fmtEta(rec?.eta_s)} label="ETA to gate" />
        <Stat value={fmtSpeed(rec?.speed_kmh)} unit="km/h" label="Speed" />
        <Stat
          value={rec?.remaining_km != null ? rec.remaining_km.toFixed(1) : "—"}
          unit="km"
          label="Remaining"
        />
      </div>

      {/* Parking availability inside the geo-fenced port (SCOPE-R1 / IU2). */}
      {parking && parking.available != null ? (
        <div className="stat-row" style={{ marginBottom: 12 }}>
          <Stat
            value={String(parking.available)}
            unit={parking.capacity != null ? `/ ${parking.capacity}` : ""}
            label="Port parking free"
          />
        </div>
      ) : null}

      {err ? <div className="banner warn">{err}</div> : null}

      {/* Traffic ahead mini-map */}
      <Card title="Traffic ahead">
        <Suspense fallback={<div className="minimap" />}>
          <MiniMap
            corridor={corridor}
            gates={gates}
            truck={pos}
            targetGateId={rec?.gate_id ?? undefined}
          />
        </Suspense>
        <div style={{ display: "flex", gap: 14, fontSize: 11, color: "var(--muted)" }}>
          <span>● Target gate</span>
          <span style={{ color: "#f0e442" }}>● You</span>
        </div>
      </Card>

      {/* Trip detail */}
      <Card title="Trip">
        {rec ? (
          <>
            <div className="row">
              <span className="k">Target gate</span>
              <span className="v">{gateShort(rec.gate_id)}</span>
            </div>
            <div className="row">
              <span className="k">State</span>
              <span className="v">{rec.state}</span>
            </div>
            <div className="row">
              <span className="k">Plate</span>
              <span className="v">{rec.plate ?? "—"}</span>
            </div>
            <div className="row">
              <span className="k">Remaining</span>
              <span className="v">{fmtKm(rec.remaining_km)}</span>
            </div>
          </>
        ) : (
          <div style={{ color: "var(--muted)", fontSize: 13 }}>
            <Spinner /> Loading trip…
          </div>
        )}
      </Card>
    </>
  );
}
