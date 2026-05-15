const HEARTBEAT_INTERVAL_MS = 20000;
const GUEST_TOKEN_STORAGE_KEY = "triviadorGuestToken";
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 10000;

function normalizeWebSocketBase(base) {
  if (!base) return null;
  if (base.startsWith("ws://") || base.startsWith("wss://")) {
    return base.replace(/\/$/, "");
  }
  if (base.startsWith("https://")) {
    return `wss://${base.slice("https://".length).replace(/\/$/, "")}`;
  }
  if (base.startsWith("http://")) {
    return `ws://${base.slice("http://".length).replace(/\/$/, "")}`;
  }
  return base.replace(/\/$/, "");
}

function resolveReconnectDelay(attemptNumber) {
  return Math.min(RECONNECT_BASE_DELAY_MS * 2 ** Math.max(0, attemptNumber - 1), RECONNECT_MAX_DELAY_MS);
}

function buildWebSocketUrl(path) {
  const explicitBase = normalizeWebSocketBase(import.meta.env.VITE_WS_BASE);
  const explicitPort = import.meta.env.VITE_WS_PORT || "3000";
  const guestToken = window.sessionStorage.getItem(GUEST_TOKEN_STORAGE_KEY);
  const separator = path.includes("?") ? "&" : "?";
  const pathWithGuestToken = guestToken ? `${path}${separator}guest_token=${encodeURIComponent(guestToken)}` : path;
  if (explicitBase) {
    return `${explicitBase}${pathWithGuestToken}`;
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const hostname = window.location.hostname || "localhost";
  return `${protocol}://${hostname}:${explicitPort}${pathWithGuestToken}`;
}

export function createSocketClient(path, handlers, options = {}) {
  const autoReconnect = options.autoReconnect ?? true;
  let heartbeatId = null;
  let socket = null;
  let reconnectTimerId = null;
  let reconnectAttempt = 0;
  let manuallyClosed = false;

  const client = {
    send(data) {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(data);
      }
    },
    close() {
      manuallyClosed = true;
      if (reconnectTimerId !== null) {
        window.clearTimeout(reconnectTimerId);
        reconnectTimerId = null;
      }
      if (heartbeatId !== null) {
        window.clearInterval(heartbeatId);
        heartbeatId = null;
      }
      socket?.close();
    },
    get readyState() {
      return socket?.readyState ?? WebSocket.CLOSED;
    },
  };

  function connect() {
    socket = new WebSocket(buildWebSocketUrl(path));

    socket.addEventListener("open", () => {
      const wasReconnecting = reconnectAttempt > 0;
      reconnectAttempt = 0;
      if (reconnectTimerId !== null) {
        window.clearTimeout(reconnectTimerId);
        reconnectTimerId = null;
      }
      handlers.onOpen?.(client, { reconnected: wasReconnecting });
      heartbeatId = window.setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ action: "ping" }));
        }
      }, HEARTBEAT_INTERVAL_MS);
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        handlers.onMessage?.(payload, client);
      } catch (error) {
        handlers.onError?.(error, { reconnecting: reconnectTimerId !== null });
      }
    });

    socket.addEventListener("close", (event) => {
      if (heartbeatId !== null) {
        window.clearInterval(heartbeatId);
        heartbeatId = null;
      }

      const shouldReconnect = autoReconnect && !manuallyClosed;
      handlers.onClose?.(event, { willReconnect: shouldReconnect });

      if (!shouldReconnect) {
        return;
      }

      reconnectAttempt += 1;
      const delayMs = resolveReconnectDelay(reconnectAttempt);
      handlers.onReconnect?.({ attempt: reconnectAttempt, delayMs });
      reconnectTimerId = window.setTimeout(() => {
        reconnectTimerId = null;
        connect();
      }, delayMs);
    });

    socket.addEventListener("error", (event) => {
      handlers.onError?.(event, { reconnecting: reconnectTimerId !== null || reconnectAttempt > 0 });
    });
  }

  connect();
  return client;
}
