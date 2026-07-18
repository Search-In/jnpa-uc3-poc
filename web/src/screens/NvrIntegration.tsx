// NVR Integration (Feature 7) — surfaces the NVR ingestion adapter: integration
// health (LIVE when NVR_BASE_URL is configured, else MOCK), the NVR device
// census, per-device channel→camera mappings, and the derived RTSP/stream
// catalogue. Also lets an operator register an NVR and map a channel to a
// camera. Backed entirely by existing /api/nvr/* endpoints — stream_url values
// are metadata only; no live video is pulled here.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Video, Plus, Server, Link2 } from "lucide-react";
import { api } from "@/lib/api";
import { PageContainer, PageHeader, StatusChip, type Tone } from "@/components/ui/dtccc";
import { Card } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/misc";
import { STATUS } from "@/lib/tokens";

function statusTone(status?: string): Tone {
  const s = (status ?? "").toUpperCase();
  if (s === "ONLINE") return "ok";
  if (s === "OFFLINE") return "critical";
  return "neutral";
}

const inputCls = "rounded-md border border-border bg-card px-2 py-1.5 text-[13px] outline-none";

export default function NvrIntegration() {
  const qc = useQueryClient();

  const healthQ = useQuery({ queryKey: ["nvr-health"], queryFn: () => api.nvrHealth(), retry: false });
  const devicesQ = useQuery({ queryKey: ["nvr-devices"], queryFn: () => api.nvrDevices() });
  const streamsQ = useQuery({ queryKey: ["nvr-streams"], queryFn: () => api.nvrStreams() });

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const detailQ = useQuery({
    queryKey: ["nvr-device", selectedId],
    queryFn: () => api.nvrDevice(selectedId as string),
    enabled: !!selectedId,
  });

  // --- Register NVR form ---
  const [reg, setReg] = useState<Record<string, string>>({
    id: "",
    name: "",
    vendor: "",
    host: "",
    port: "554",
    protocol: "RTSP",
    channels: "",
  });
  const register = useMutation({
    mutationFn: () =>
      api.nvrRegister({
        id: reg.id.trim(),
        name: reg.name.trim(),
        vendor: reg.vendor.trim(),
        host: reg.host.trim(),
        port: reg.port ? Number(reg.port) : undefined,
        protocol: reg.protocol,
        channels: reg.channels ? Number(reg.channels) : undefined,
      }),
    onSuccess: () => {
      setReg({ id: "", name: "", vendor: "", host: "", port: "554", protocol: "RTSP", channels: "" });
      qc.invalidateQueries({ queryKey: ["nvr-devices"] });
      qc.invalidateQueries({ queryKey: ["nvr-health"] });
      qc.invalidateQueries({ queryKey: ["nvr-streams"] });
    },
  });

  // --- Map channel form ---
  const [map, setMap] = useState<Record<string, string>>({ id: "", channel: "", camera_id: "" });
  const mapChannel = useMutation({
    mutationFn: () =>
      api.nvrMapChannel(map.id.trim(), {
        channel: map.channel ? Number(map.channel) : map.channel,
        camera_id: map.camera_id.trim(),
      }),
    onSuccess: () => {
      setMap({ id: "", channel: "", camera_id: "" });
      qc.invalidateQueries({ queryKey: ["nvr-devices"] });
      qc.invalidateQueries({ queryKey: ["nvr-device", selectedId] });
      qc.invalidateQueries({ queryKey: ["nvr-streams"] });
    },
  });

  const health: any = healthQ.data;
  const configured = !!health?.configured;
  const devices: any[] = devicesQ.data?.devices ?? [];
  const streams: any[] = streamsQ.data?.streams ?? [];
  const detail: any = detailQ.data;
  const mappings: any[] = detail?.channels ?? detail?.channel_mappings ?? detail?.mappings ?? [];

  const canRegister =
    reg.id.trim() && reg.name.trim() && reg.host.trim() && !register.isPending;
  const canMap = map.id.trim() && map.channel !== "" && map.camera_id.trim() && !mapChannel.isPending;

  return (
    <PageContainer>
      <PageHeader
        icon={Video}
        title="NVR Integration"
        subtitle="Network Video Recorder ingestion — devices · channels · stream catalogue"
        actions={
          healthQ.isLoading ? (
            <StatusChip label="…" tone="neutral" />
          ) : configured ? (
            <StatusChip label={`LIVE${health?.mode ? ` · ${health.mode}` : ""}`} tone="ok" />
          ) : (
            <StatusChip label="MOCK — NVR_BASE_URL not configured" tone="warn" />
          )
        }
        onRefresh={() => {
          void healthQ.refetch();
          void devicesQ.refetch();
          void streamsQ.refetch();
        }}
      />

      <div className="space-y-3 px-4 py-3">
        {/* ---------------- Integration health census ---------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <Server size={15} />
            <h3 className="text-sm font-semibold">Integration health</h3>
            {health?.system && (
              <span className="text-[11px] text-muted-foreground">{health.system}</span>
            )}
          </div>
          <div className="flex flex-wrap gap-4 text-[13px]">
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Mode</div>
              <div className="font-medium">{health?.mode ?? (configured ? "LIVE" : "MOCK")}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Devices</div>
              <div className="font-medium tabular-nums">{devicesQ.data?.count ?? devices.length}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Online</div>
              <div className="font-medium tabular-nums" style={{ color: STATUS.ok }}>
                {health?.online ?? "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Offline</div>
              <div className="font-medium tabular-nums" style={{ color: STATUS.critical }}>
                {health?.offline ?? "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Streams</div>
              <div className="font-medium tabular-nums">{streamsQ.data?.count ?? streams.length}</div>
            </div>
          </div>
        </Card>

        {/* ---------------- NVR devices table ---------------- */}
        <Card className="p-4">
          <div className="mb-3 flex items-center gap-2">
            <h3 className="text-sm font-semibold">NVR devices</h3>
            <span className="text-[11px] text-muted-foreground">
              ({devicesQ.data?.count ?? devices.length})
            </span>
          </div>
          {devicesQ.isLoading ? (
            <LoadingState />
          ) : devices.length === 0 ? (
            <EmptyState>No NVR devices registered — add one below.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">Status</th>
                    <th className="py-1 pr-3 font-medium">ID</th>
                    <th className="py-1 pr-3 font-medium">Name</th>
                    <th className="py-1 pr-3 font-medium">Vendor</th>
                    <th className="py-1 pr-3 font-medium">Host:Port</th>
                    <th className="py-1 pr-3 font-medium">Protocol</th>
                    <th className="py-1 pr-3 font-medium">Channels</th>
                  </tr>
                </thead>
                <tbody>
                  {devices.map((d) => (
                    <tr
                      key={d.id}
                      onClick={() => setSelectedId(d.id)}
                      className={`cursor-pointer border-t border-border align-top hover:bg-muted/50 ${
                        selectedId === d.id ? "bg-muted/60" : ""
                      }`}
                    >
                      <td className="py-1.5 pr-3">
                        <StatusChip label={d.status ?? "UNKNOWN"} tone={statusTone(d.status)} />
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-[11px]">{d.id}</td>
                      <td className="py-1.5 pr-3 font-medium">{d.name}</td>
                      <td className="py-1.5 pr-3">{d.vendor ?? "—"}</td>
                      <td className="py-1.5 pr-3 font-mono text-[11px]">
                        {d.host}
                        {d.port != null ? `:${d.port}` : ""}
                      </td>
                      <td className="py-1.5 pr-3">{d.protocol ?? "—"}</td>
                      <td className="py-1.5 pr-3 tabular-nums">
                        {d.channel_count ?? d.channels ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Selected device detail */}
          {selectedId && (
            <div className="mt-3 rounded-md border border-border bg-muted/30 p-3">
              <div className="mb-2 flex items-center justify-between">
                <h4 className="text-[13px] font-semibold">
                  Device detail · <span className="font-mono">{selectedId}</span>
                </h4>
                <button
                  onClick={() => setSelectedId(null)}
                  className="text-[11px] text-muted-foreground hover:text-foreground"
                >
                  close
                </button>
              </div>
              {detailQ.isLoading ? (
                <LoadingState />
              ) : !detail ? (
                <EmptyState>No detail available.</EmptyState>
              ) : (
                <>
                  <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
                    <span>
                      <span className="text-muted-foreground">Name:</span> {detail.name ?? "—"}
                    </span>
                    <span>
                      <span className="text-muted-foreground">Vendor:</span> {detail.vendor ?? "—"}
                    </span>
                    <span>
                      <span className="text-muted-foreground">Host:</span> {detail.host ?? "—"}
                      {detail.port != null ? `:${detail.port}` : ""}
                    </span>
                    <span>
                      <span className="text-muted-foreground">Protocol:</span>{" "}
                      {detail.protocol ?? "—"}
                    </span>
                    <StatusChip label={detail.status ?? "UNKNOWN"} tone={statusTone(detail.status)} />
                  </div>
                  <div className="text-[11px] font-semibold text-muted-foreground">
                    Channel mappings ({mappings.length})
                  </div>
                  {mappings.length === 0 ? (
                    <p className="text-[12px] text-muted-foreground">No channel mappings.</p>
                  ) : (
                    <ul className="mt-1 space-y-1">
                      {mappings.map((m, i) => (
                        <li key={i} className="font-mono text-[11px]">
                          ch {m.channel} → {m.camera_id ?? "(unmapped)"}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </div>
          )}
        </Card>

        {/* ---------------- Register NVR + Map channel ---------------- */}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Card className="p-4">
            <div className="mb-3 flex items-center gap-2">
              <Plus size={15} />
              <h3 className="text-sm font-semibold">Register NVR</h3>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">ID</span>
                <input
                  value={reg.id}
                  onChange={(e) => setReg((s) => ({ ...s, id: e.target.value }))}
                  placeholder="NVR-01"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Name</span>
                <input
                  value={reg.name}
                  onChange={(e) => setReg((s) => ({ ...s, name: e.target.value }))}
                  placeholder="Gate-3 recorder"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Vendor</span>
                <input
                  value={reg.vendor}
                  onChange={(e) => setReg((s) => ({ ...s, vendor: e.target.value }))}
                  placeholder="Hikvision"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Host</span>
                <input
                  value={reg.host}
                  onChange={(e) => setReg((s) => ({ ...s, host: e.target.value }))}
                  placeholder="10.0.0.20"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Port</span>
                <input
                  value={reg.port}
                  onChange={(e) => setReg((s) => ({ ...s, port: e.target.value }))}
                  placeholder="554"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Protocol</span>
                <select
                  value={reg.protocol}
                  onChange={(e) => setReg((s) => ({ ...s, protocol: e.target.value }))}
                  className={inputCls}
                >
                  <option value="RTSP">RTSP</option>
                  <option value="ONVIF">ONVIF</option>
                  <option value="HTTP">HTTP</option>
                </select>
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Channels</span>
                <input
                  value={reg.channels}
                  onChange={(e) => setReg((s) => ({ ...s, channels: e.target.value }))}
                  placeholder="16"
                  className={inputCls}
                />
              </label>
            </div>
            <button
              disabled={!canRegister}
              onClick={() => register.mutate()}
              className="mt-3 rounded-md bg-primary px-3 py-1.5 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {register.isPending ? "Registering…" : "Register NVR"}
            </button>
            {register.isError && (
              <div className="mt-2 text-[11px]" style={{ color: STATUS.critical }}>
                {(register.error as Error)?.message}
              </div>
            )}
            {register.isSuccess && (
              <div className="mt-2 text-[11px]" style={{ color: STATUS.ok }}>
                NVR registered.
              </div>
            )}
          </Card>

          <Card className="p-4">
            <div className="mb-3 flex items-center gap-2">
              <Link2 size={15} />
              <h3 className="text-sm font-semibold">Map channel</h3>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">NVR ID</span>
                <input
                  value={map.id}
                  onChange={(e) => setMap((s) => ({ ...s, id: e.target.value }))}
                  placeholder="NVR-01"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Channel</span>
                <input
                  value={map.channel}
                  onChange={(e) => setMap((s) => ({ ...s, channel: e.target.value }))}
                  placeholder="1"
                  className={inputCls}
                />
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-muted-foreground">Camera ID</span>
                <input
                  value={map.camera_id}
                  onChange={(e) => setMap((s) => ({ ...s, camera_id: e.target.value }))}
                  placeholder="CAM-GATE-3"
                  className={inputCls}
                />
              </label>
            </div>
            <button
              disabled={!canMap}
              onClick={() => mapChannel.mutate()}
              className="mt-3 rounded-md border border-border px-3 py-1.5 text-[13px] font-semibold hover:bg-muted disabled:opacity-50"
            >
              {mapChannel.isPending ? "Mapping…" : "Map channel → camera"}
            </button>
            {mapChannel.isError && (
              <div className="mt-2 text-[11px]" style={{ color: STATUS.critical }}>
                {(mapChannel.error as Error)?.message}
              </div>
            )}
            {mapChannel.isSuccess && (
              <div className="mt-2 text-[11px]" style={{ color: STATUS.ok }}>
                Channel mapped.
              </div>
            )}
          </Card>
        </div>

        {/* ---------------- Stream catalogue ---------------- */}
        <Card className="p-4">
          <div className="mb-1 flex items-center gap-2">
            <h3 className="text-sm font-semibold">Stream catalogue</h3>
            <span className="text-[11px] text-muted-foreground">
              ({streamsQ.data?.count ?? streams.length})
            </span>
          </div>
          <p className="mb-3 text-[11px] text-muted-foreground">
            stream_url is metadata only — this console does not pull live video.
          </p>
          {streamsQ.isLoading ? (
            <LoadingState />
          ) : streams.length === 0 ? (
            <EmptyState>No streams derived yet — register an NVR and map channels.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] border-collapse text-[12px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">NVR</th>
                    <th className="py-1 pr-3 font-medium">Channel</th>
                    <th className="py-1 pr-3 font-medium">Camera</th>
                    <th className="py-1 pr-3 font-medium">Stream URL</th>
                    <th className="py-1 pr-3 font-medium">Codec</th>
                    <th className="py-1 pr-3 font-medium">Resolution</th>
                    <th className="py-1 pr-3 font-medium">FPS</th>
                    <th className="py-1 pr-3 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {streams.map((s, i) => (
                    <tr key={`${s.nvr_id}-${s.channel}-${i}`} className="border-t border-border align-top">
                      <td className="py-1.5 pr-3">
                        <div className="font-medium">{s.nvr_name ?? s.nvr_id}</div>
                        <div className="font-mono text-[10px] text-muted-foreground">{s.nvr_id}</div>
                      </td>
                      <td className="py-1.5 pr-3 tabular-nums">{s.channel}</td>
                      <td className="py-1.5 pr-3 font-mono text-[11px]">{s.camera_id ?? "—"}</td>
                      <td className="py-1.5 pr-3">
                        <span className="break-all font-mono text-[11px]" title={s.stream_url}>
                          {s.stream_url ?? "—"}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3">{s.codec ?? "—"}</td>
                      <td className="py-1.5 pr-3">{s.resolution ?? "—"}</td>
                      <td className="py-1.5 pr-3 tabular-nums">{s.fps ?? "—"}</td>
                      <td className="py-1.5 pr-3">
                        <StatusChip label={s.status ?? "UNKNOWN"} tone={statusTone(s.status)} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}
