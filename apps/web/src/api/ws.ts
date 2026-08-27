import { useEffect, useRef } from "react";

/**
 * Subscribe to the server-side EventBus over /api/v1/ws.
 *
 * Auto-reconnects with exponential backoff on close/error, and
 * polls a fallback every 5s while disconnected so the UI stays
 * responsive when the WebSocket can't establish (corporate proxy,
 * mobile network drop, CloudFront idle timeout, etc.).
 *
 * The hook does NOT diff events — every push is forwarded to
 * `onEvent`. The page-level handler is responsible for invalidating
 * the right query keys. Synthetic events emitted by this hook:
 *   { type: "ws.reconnected" }   — first message after a reconnect
 *   { type: "ws.polling.tick" }  — fallback heartbeat while WS is down
 */
export function useEventStream(onEvent: (ev: unknown) => void) {
  // Ref so reconnect logic can read the latest callback without
  // triggering effect re-runs (which would tear down the WS).
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let pollTimer: number | null = null;
    let pingTimer: number | null = null;
    let pongTimeout: number | null = null;
    let pingId = 0;
    let attempts = 0;
    let cancelled = false;

    const url =
      (location.protocol === "https:" ? "wss://" : "ws://") +
      location.host +
      "/api/v1/ws";

    // Application-level heartbeat. Industry-standard pattern (Slack
    // RTM, Discord Gateway, GitHub live-logs): client sends a ping
    // every PING_EVERY_MS; if no pong arrives within PONG_DEADLINE_MS
    // the connection is presumed half-open (CloudFront / corporate
    // proxy silently dropped the TCP without delivering FIN), and we
    // close it to trigger the existing reconnect path. Without this
    // the browser believes the socket is alive long after it isn't.
    const PING_EVERY_MS = 25_000;
    const PONG_DEADLINE_MS = 10_000;

    const startPollingFallback = () => {
      if (pollTimer !== null) return;
      pollTimer = window.setInterval(() => {
        cbRef.current({ type: "ws.polling.tick" });
      }, 5000);
    };

    const stopPollingFallback = () => {
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const stopHeartbeat = () => {
      if (pingTimer !== null) {
        window.clearInterval(pingTimer);
        pingTimer = null;
      }
      if (pongTimeout !== null) {
        window.clearTimeout(pongTimeout);
        pongTimeout = null;
      }
    };

    const startHeartbeat = (sock: WebSocket) => {
      stopHeartbeat();
      pingTimer = window.setInterval(() => {
        if (sock.readyState !== WebSocket.OPEN) return;
        pingId += 1;
        const myId = pingId;
        try {
          sock.send(JSON.stringify({ type: "ping", id: myId }));
        } catch {
          return;
        }
        // Force close if the server hasn't responded to *some* pong
        // within PONG_DEADLINE_MS. Each new ping resets the timer
        // (the in-flight one is cleared in onmessage).
        if (pongTimeout !== null) window.clearTimeout(pongTimeout);
        pongTimeout = window.setTimeout(() => {
          // Silent half-open detected — close so onclose triggers
          // the reconnect machinery.
          try {
            sock.close();
          } catch {
            /* ignore */
          }
        }, PONG_DEADLINE_MS);
      }, PING_EVERY_MS);
    };

    const connect = () => {
      if (cancelled) return;
      ws = new WebSocket(url);

      ws.onopen = () => {
        attempts = 0;
        stopPollingFallback();
        startHeartbeat(ws!);
        // After a reconnect, fire a synthetic event so the UI
        // catches up on anything missed during the disconnect.
        cbRef.current({ type: "ws.reconnected" });
      };

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data as string);
          // Pong handler — clear the deadline so it doesn't kill
          // the connection. Don't bubble pong events up to the page.
          if (data && (data as { type?: string }).type === "pong") {
            if (pongTimeout !== null) {
              window.clearTimeout(pongTimeout);
              pongTimeout = null;
            }
            return;
          }
          cbRef.current(data);
        } catch {
          // ignore parse errors; not actionable.
        }
      };

      const scheduleReconnect = () => {
        if (cancelled) return;
        stopHeartbeat();
        startPollingFallback();
        // Exponential backoff capped at 30s.
        // 0→0.5s, 1→1s, 2→2s, 3→4s, 4→8s, 5→16s, 6+→30s.
        const delay = Math.min(500 * 2 ** attempts, 30_000);
        attempts += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };

      ws.onerror = () => {
        // 'close' handler runs immediately after error; let it own
        // the reconnect bookkeeping.
      };

      ws.onclose = () => {
        ws = null;
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      stopHeartbeat();
      stopPollingFallback();
      if (ws !== null) {
        ws.close();
      }
    };
    // Deliberately empty deps: we want a single WS that survives
    // re-renders. Latest callback reached via cbRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
