// Realtime worker: owns the WebSocket to wss://gateway/api/ws and relays frames
// to the page. Running the socket off the main thread keeps the UI thread free
// (helps the FCP / Lighthouse targets) and survives transient main-thread jank.
//
// Protocol (postMessage):
//   page -> worker: { cmd: "connect", url, deviceId } | { cmd: "close" }
//   worker -> page: { kind: "status", status } | { kind: "frame", frame }
//
// On open the worker sends {cmd:"identify", device_id} so the gateway can bind
// this socket to the paired device and stop sending it other drivers' advisories
// (the gateway also reads ?device= from the URL, which binds without the
// round-trip; identify covers a reconnect that raced the query param).
//
// It then drops any frame ADDRESSED to a different device — truck_position,
// reroute, alert, everything. A frame is "addressed" when payload.device_id is
// set; a frame with audience:"broadcast" is for every driver and always passes.
// This is the transport-level half of the isolation fix; RealtimeContext applies
// the same rule again before raising a notification.

import { isForOtherDevice } from "@/lib/addressing";

let ws: WebSocket | null = null;
let deviceId = "";
let retry = 0;
let pingTimer: ReturnType<typeof setInterval> | undefined;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let url = "";
let closedByPage = false;

function post(msg: unknown) {
  (self as unknown as Worker).postMessage(msg);
}

function connect() {
  post({ kind: "status", status: "connecting" });
  try {
    ws = new WebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    retry = 0;
    post({ kind: "status", status: "open" });
    // Bind this socket to our device so the gateway can address frames to it.
    if (deviceId) {
      try {
        ws?.send(JSON.stringify({ cmd: "identify", device_id: deviceId }));
      } catch {
        /* socket raced closed — the ?device= query param already bound it */
      }
    }
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 25_000);
  };

  ws.onmessage = (ev) => {
    let frame: any;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    // Drop anything addressed to a different device before it reaches the page.
    if (isForOtherDevice(frame?.payload, deviceId)) return;
    post({ kind: "frame", frame });
  };

  ws.onerror = () => ws?.close();
  ws.onclose = () => {
    if (pingTimer) clearInterval(pingTimer);
    post({ kind: "status", status: "closed" });
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (closedByPage) return;
  const delay = Math.min(1000 * 2 ** retry, 15_000);
  retry += 1;
  reconnectTimer = setTimeout(connect, delay);
}

self.onmessage = (ev: MessageEvent) => {
  const msg = ev.data || {};
  if (msg.cmd === "connect") {
    closedByPage = false;
    url = msg.url;
    deviceId = msg.deviceId || "";
    if (ws) ws.close();
    connect();
  } else if (msg.cmd === "close") {
    closedByPage = true;
    if (pingTimer) clearInterval(pingTimer);
    if (reconnectTimer) clearTimeout(reconnectTimer);
    ws?.close();
    ws = null;
  }
};

export {};
