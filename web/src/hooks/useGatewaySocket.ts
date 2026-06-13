import { useEffect, useRef, useState, useCallback } from "react";
import type { WsFrame } from "@/lib/types";

type Status = "connecting" | "open" | "closed";

// One shared WebSocket to /api/ws fanning out alert / traffic / truck_position /
// decision frames. The hook auto-reconnects with capped backoff and keeps the
// socket alive with a periodic ping (the server reads inbound text only to
// detect disconnects). Consumers subscribe by frame type.

function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/api/ws`;
}

type Listener = (frame: WsFrame) => void;

export function useGatewaySocket() {
  const [status, setStatus] = useState<Status>("connecting");
  const listeners = useRef<Set<Listener>>(new Set());
  const wsRef = useRef<WebSocket | null>(null);
  const retry = useRef(0);
  const closed = useRef(false);

  const subscribe = useCallback((fn: Listener) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  useEffect(() => {
    closed.current = false;
    let pingTimer: number | undefined;
    let reconnectTimer: number | undefined;

    const connect = () => {
      setStatus("connecting");
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        retry.current = 0;
        setStatus("open");
        pingTimer = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 25_000);
      };
      ws.onmessage = (ev) => {
        try {
          const frame = JSON.parse(ev.data) as WsFrame;
          listeners.current.forEach((fn) => fn(frame));
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onerror = () => ws.close();
      ws.onclose = () => {
        window.clearInterval(pingTimer);
        setStatus("closed");
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (closed.current) return;
      const delay = Math.min(1000 * 2 ** retry.current, 15_000);
      retry.current += 1;
      reconnectTimer = window.setTimeout(connect, delay);
    };

    connect();
    return () => {
      closed.current = true;
      window.clearInterval(pingTimer);
      window.clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, []);

  return { status, subscribe };
}
