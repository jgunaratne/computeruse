import type { Outcome, SessionStatus } from "../types";

export const fmtDuration = (ms: number | null | undefined): string => {
  if (ms == null) return "–";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  return m < 60 ? `${m}m ${rest.toString().padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
};

export const fmtMs = (ms: number | null | undefined): string => (ms == null ? "–" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);

export const fmtCost = (usd: number | null | undefined): string => (usd == null ? "–" : usd < 0.01 && usd > 0 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`);

export const fmtPct = (r: number | null | undefined, digits = 0): string => (r == null ? "–" : `${(r * 100).toFixed(digits)}%`);

export const fmtTime = (ts: number | null | undefined): string => {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

export const fmtRelative = (ts: number | null | undefined): string => {
  if (!ts) return "–";
  const diff = Date.now() / 1000 - ts;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
};

export const fmtClock = (ts: number) => {
  const d = new Date(ts * 1000);
  return `${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}.${String(d.getMilliseconds()).padStart(3, "0").slice(0, 1)}`;
};

export const statusLabel: Record<SessionStatus, string> = {
  created: "Created",
  starting: "Starting",
  running: "Running",
  paused: "Paused",
  awaiting_approval: "Needs approval",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const outcomeLabel: Record<Outcome, string> = {
  completed: "Completed",
  max_steps: "Step budget exhausted",
  timeout: "Timed out",
  budget_exceeded: "Cost budget exceeded",
  stuck: "Stuck (no screen change)",
  model_error: "Model error",
  computer_error: "Computer error",
  guardrail_blocked: "Blocked by guardrails",
  cancelled: "Cancelled",
  internal_error: "Internal error",
};

export const statusTone = (s: SessionStatus): "ok" | "warn" | "bad" | "muted" | "live" => {
  switch (s) {
    case "running":
    case "starting":
      return "live";
    case "paused":
    case "awaiting_approval":
      return "warn";
    case "completed":
      return "ok";
    case "failed":
      return "bad";
    default:
      return "muted";
  }
};

export const truncate = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export const classNames = (...xs: (string | false | null | undefined)[]) => xs.filter(Boolean).join(" ");
