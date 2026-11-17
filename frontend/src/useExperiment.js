import { useCallback, useEffect, useRef, useState } from "react";

// sessionStorage (not localStorage) so each browser tab is its own participant.
const TOKEN_KEY = "hae_participant_token";

function readToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
function writeToken(t) {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: reconnect just won't restore the seat */
  }
}

export function useExperiment() {
  const [state, setState] = useState(null);
  const [messages, setMessages] = useState([]);
  const [connected, setConnected] = useState(false);
  const [clockOffset, setClockOffset] = useState(0);
  const wsRef = useRef(null);
  const stopRef = useRef(false);

  useEffect(() => {
    stopRef.current = false;
    let retry;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        ws.send(JSON.stringify({ type: "join", token: readToken() }));
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        switch (msg.type) {
          case "welcome":
            writeToken(msg.token);
            break;
          case "history":
            setMessages(msg.messages);
            break;
          case "chat":
            setMessages((prev) =>
              prev.some((m) => m.id === msg.message.id) ? prev : [...prev, msg.message]
            );
            break;
          case "state":
            setClockOffset(msg.server_time - Date.now());
            setState(msg);
            break;
          default:
            break;
        }
      };
      ws.onclose = (ev) => {
        setConnected(false);
        // 4000 = replaced by a newer connection from the same participant.
        if (!stopRef.current && ev.code !== 4000) retry = setTimeout(connect, 1500);
      };
    };

    connect();
    return () => {
      stopRef.current = true;
      clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback((payload) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
  }, []);

  const sendChat = useCallback((text) => send({ type: "chat", text }), [send]);
  const vote = useCallback((target) => send({ type: "vote", target }), [send]);
  const leave = useCallback(() => {
    writeToken(null);
    window.location.reload();
  }, []);

  return { state, messages, connected, clockOffset, sendChat, vote, leave };
}

export function useCountdown(deadline, clockOffset) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!deadline) return undefined;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [deadline]);
  if (!deadline) return null;
  return Math.max(0, Math.ceil((deadline - (now + clockOffset)) / 1000));
}
