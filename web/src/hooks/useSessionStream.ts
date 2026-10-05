import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../api";
import type { Event, InputAction, Session } from "../types";

export interface InputResult {
  id: number | null;
  action?: string;
  ok: boolean;
  error?: string | null;
  /** Client receive time (ms). */
  at: number;
}

export interface SessionStream {
  session: Session | null;
  events: Event[];
  /** Latest live preview frame as a data URL (only while the session is active and watched). */
  preview: string | null;
  previewAt: number;
  connected: boolean;
  ended: boolean;
  error: string | null;
  /** Most recent failed operator input (surfaced as a toast by the page). */
  inputError: InputResult | null;
  /**
   * Send one raw operator input over the socket (the operator must hold control).
   * Returns false when not connected. Pass an id to get an `input_result` back for it.
   */
  sendInput: (action: InputAction, id?: number) => boolean;
}

type WsMessage =
  | { type: "history"; session: Session; events: Event[] }
  | { type: "event"; event: Event; session?: Session }
  | { type: "heartbeat"; session?: Session | null }
  | { type: "end"; session: Session }
  | { type: "pong" }
  | { type: "input_result"; id: number | null; action?: string; ok: boolean; error?: string | null };

/** Streams one session: history on connect, then live events + preview frames. Reconnects while active. */
export function useSessionStream(sessionId: string): SessionStream {
  const [state, setState] = useState<Omit<SessionStream, "sendInput">>({
    session: null,
    events: [],
    preview: null,
    previewAt: 0,
    connected: false,
    ended: false,
    error: null,
    inputError: null,
  });
  const alive = useRef(true);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    alive.current = true;
    let ws: WebSocket | null = null;
    let attempt = 0;
    let timer: number | undefined;
    setState({ session: null, events: [], preview: null, previewAt: 0, connected: false, ended: false, error: null, inputError: null });

    const connect = () => {
      ws = new WebSocket(wsUrl(`/api/sessions/${sessionId}/ws`));
      wsRef.current = ws;
      ws.onopen = () => {
        attempt = 0;
        setState((s) => ({ ...s, connected: true, error: null }));
      };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data) as WsMessage;
        if (msg.type === "history") {
          setState((s) => ({ ...s, session: msg.session, events: msg.events }));
        } else if (msg.type === "event") {
          const ev = msg.event;
          if (ev.type === "preview") {
            setState((s) => ({ ...s, preview: `data:image/jpeg;base64,${ev.data.jpeg_b64}`, previewAt: ev.ts }));
          } else {
            setState((s) => ({
              ...s,
              events: s.events.length && s.events[s.events.length - 1].seq >= ev.seq ? s.events : [...s.events, ev],
              session: msg.session ?? s.session,
            }));
          }
        } else if (msg.type === "heartbeat") {
          if (msg.session) setState((s) => ({ ...s, session: msg.session ?? s.session }));
        } else if (msg.type === "end") {
          setState((s) => ({ ...s, session: msg.session, ended: true, preview: null }));
        } else if (msg.type === "input_result") {
          if (!msg.ok) setState((s) => ({ ...s, inputError: { id: msg.id, action: msg.action, ok: false, error: msg.error, at: Date.now() } }));
        }
      };
      ws.onclose = (ev) => {
        if (wsRef.current === ws) wsRef.current = null;
        if (!alive.current) return;
        setState((s) => {
          const terminal = s.ended || s.session?.terminal;
          if (!terminal && ev.code !== 4404) {
            const delay = Math.min(10000, 500 * 2 ** attempt++);
            timer = window.setTimeout(connect, delay);
          }
          return { ...s, connected: false, error: ev.code === 4404 ? "Unknown session" : s.error };
        });
      };
      ws.onerror = () => {
        /* onclose follows */
      };
    };
    connect();

    // Fallback for the rare case where a terminal session's history arrives but no `end` frame does.
    const poll = window.setInterval(async () => {
      setState((s) => s); // noop to keep closure fresh
      try {
        const sess = await api.session(sessionId);
        setState((s) => (s.session && !s.session.terminal && sess.terminal ? { ...s, session: sess, ended: true } : s));
      } catch {
        /* ignore */
      }
    }, 15000);

    return () => {
      alive.current = false;
      window.clearTimeout(timer);
      window.clearInterval(poll);
      ws?.close();
      wsRef.current = null;
    };
  }, [sessionId]);

  const sendInput = useCallback((action: InputAction, id?: number): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(id == null ? { type: "input", action } : { type: "input", action, id }));
    return true;
  }, []);

  return { ...state, sendInput };
}
