// Realtime worker: owns the WebSocket to wss://gateway/api/ws and relays frames
// to the page. Running the socket off the main thread keeps the UI thread free
// (helps the FCP / Lighthouse targets) and survives transient main-thread jank.
//
// Protocol (postMessage):
//   page -> worker: { cmd: "connect", url, deviceId } | { cmd: "close" }
//   worker -> page: { kind: "status", status } | { kind: "frame", frame }
//
// The worker filters truck_position frames to the paired device_id (those are
// high-volume and 1-in-50 sampled by the gateway); all other frame types pass
// through and the page decides relevance. It auto-reconnects with capped
// backoff and pings to keep the socket alive.

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
    // Drop other devices' position spam; keep everything else.
    if (
      frame?.type === "truck_position" &&
      deviceId &&
      frame.payload?.device_id &&
      frame.payload.device_id !== deviceId
    ) {
      return;
    }
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
