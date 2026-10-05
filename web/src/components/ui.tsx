import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { classNames, statusLabel, statusTone } from "../lib/format";
import type { Session, SessionStatus } from "../types";

export function Pill({ tone, children, mono, title }: { tone?: "ok" | "warn" | "bad" | "info" | "live" | "muted"; children: ReactNode; mono?: boolean; title?: string }) {
  return (
    <span className={classNames("pill", tone && tone !== "muted" && tone, mono && "mono")} title={title}>
      {children}
    </span>
  );
}

export function StatusPill({ status }: { status: SessionStatus }) {
  return <Pill tone={statusTone(status)}>{statusLabel[status]}</Pill>;
}

export function VerdictPill({ s }: { s: Session }) {
  if (s.eval_passed === true) return <Pill tone="ok" title={s.eval_detail ?? undefined}>verified ✓</Pill>;
  if (s.eval_passed === false)
    return (
      <Pill tone="bad" title={s.eval_detail ?? undefined}>
        {s.outcome === "completed" ? "false completion" : "verifier failed"}
      </Pill>
    );
  return null;
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Ticks every second while `active`, for elapsed-time displays. */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return now;
}

/** Simple async data hook with manual refresh. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], opts: { pollMs?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const load = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    void load();
    if (!opts.pollMs) return;
    const id = window.setInterval(load, opts.pollMs);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, load, opts.pollMs]);
  return { data, error, loading, reload: load, setData };
}

// --- toasts -----------------------------------------------------------------

type Toast = { id: number; text: string; tone?: "bad" };
const ToastCtx = createContext<(text: string, tone?: "bad") => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone?: "bad") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500);
  }, []);
  const value = useMemo(() => push, [push]);
  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={classNames("toast", t.tone)}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
