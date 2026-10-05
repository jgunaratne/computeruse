// Derives the human-facing timeline (turns → steps) from the flat event log.
import type { Event, FrameRef, Usage } from "../types";

export interface Step {
  step: number;
  turn: number;
  kind: string;
  description: string;
  coordinates: number[][];
  action: Record<string, unknown>;
  proposedAt: number;
  guardrail?: { decision: string; rule: string; reason: string };
  approval?: { approval_id: string; reason: string; approved: boolean | null };
  executed?: {
    ok: boolean;
    error: string | null;
    duration_ms: number;
    frame_seq: number | null;
    screen_changed: boolean | null;
    blocked?: boolean;
    rejected?: boolean;
    output?: string | null;
  };
  nudged?: boolean;
  /** Frame visible when the model decided on this action (before executing it). */
  frameBefore: number | null;
}

export interface Turn {
  index: number;
  startedAt: number;
  texts: string[];
  latency_ms?: number;
  usage?: Usage;
  stop_reason?: string;
  retries?: number;
  model?: string;
  steps: Step[];
}

export type Note = {
  kind: "note";
  ts: number;
  seq: number;
  tone: "info" | "warn" | "error" | "user";
  title: string;
  body?: string;
  frame_seq?: number | null;
};

export type TimelineItem = { kind: "turn"; turn: Turn; ts: number; seq: number } | Note;

export interface Timeline {
  items: TimelineItem[];
  steps: Step[];
  frames: FrameRef[];
  frameBySeq: Map<number, FrameRef>;
  display: { width: number; height: number } | null;
  config: Record<string, unknown> | null;
  ended: Record<string, unknown> | null;
  pendingApproval: { approval_id: string; description: string; reason: string; kind: string; step: number } | null;
  lastText: string | null;
  lastPreviewSeq: number;
}

export function buildTimeline(events: Event[]): Timeline {
  const items: TimelineItem[] = [];
  const turns = new Map<number, Turn>();
  const steps = new Map<number, Step>();
  const frames: FrameRef[] = [];
  const frameBySeq = new Map<number, FrameRef>();
  let display: Timeline["display"] = null;
  let config: Timeline["config"] = null;
  let ended: Timeline["ended"] = null;
  let pendingApproval: Timeline["pendingApproval"] = null;
  let lastText: string | null = null;
  let latestFrameSeq: number | null = null;
  let lastPreviewSeq = -1;

  const turnFor = (idx: number, ts: number): Turn => {
    let t = turns.get(idx);
    if (!t) {
      t = { index: idx, startedAt: ts, texts: [], steps: [] };
      turns.set(idx, t);
      items.push({ kind: "turn", turn: t, ts, seq: 0 });
    }
    return t;
  };
  const note = (e: Event, tone: Note["tone"], title: string, body?: string, frame_seq?: number | null) =>
    items.push({ kind: "note", ts: e.ts, seq: e.seq, tone, title, body, frame_seq });

  for (const e of events) {
    const d = e.data;
    switch (e.type) {
      case "session.started":
        display = d.display ?? null;
        config = d.config ?? null;
        break;
      case "frame": {
        const ref = d as FrameRef;
        frames.push(ref);
        frameBySeq.set(ref.seq, ref);
        latestFrameSeq = ref.seq;
        break;
      }
      case "turn.started":
        if (typeof d.turn === "number" && d.turn >= 0) turnFor(d.turn, e.ts);
        else if (d.phase === "eval_check")
          note(e, d.passed ? "info" : "warn", d.passed ? "Verifier passed" : "Verifier could not confirm", String(d.detail ?? ""));
        break;
      case "model.called": {
        const t = turnFor(d.turn, e.ts);
        t.latency_ms = d.latency_ms;
        t.usage = d.usage;
        t.stop_reason = d.stop_reason;
        t.retries = d.retries;
        t.model = d.model;
        break;
      }
      case "model.retry":
        note(e, "warn", "Model call retried", String(d.error ?? ""));
        break;
      case "assistant.text": {
        const t = turnFor(d.turn, e.ts);
        t.texts.push(String(d.text ?? ""));
        lastText = String(d.text ?? "");
        break;
      }
      case "action.proposed": {
        const t = turnFor(d.turn, e.ts);
        const s: Step = {
          step: d.step,
          turn: d.turn,
          kind: d.kind,
          description: d.description,
          coordinates: d.coordinates ?? [],
          action: d.action ?? {},
          proposedAt: e.ts,
          frameBefore: latestFrameSeq,
        };
        steps.set(d.step, s);
        t.steps.push(s);
        break;
      }
      case "guardrail.decision": {
        const s = steps.get(d.step);
        if (s && d.decision !== "allow") s.guardrail = { decision: d.decision, rule: d.rule, reason: d.reason };
        break;
      }
      case "approval.requested": {
        const s = steps.get(d.step);
        if (s) s.approval = { approval_id: d.approval_id, reason: d.reason, approved: null };
        pendingApproval = { approval_id: d.approval_id, description: d.description, reason: d.reason, kind: d.kind, step: d.step };
        break;
      }
      case "approval.resolved": {
        const s = steps.get(d.step);
        if (s?.approval) s.approval.approved = !!d.approved;
        pendingApproval = null;
        break;
      }
      case "action.executed": {
        let s = steps.get(d.step);
        if (!s) {
          // Invalid actions are reported as executed without a proposal.
          const t = turnFor(d.turn ?? 0, e.ts);
          s = { step: d.step, turn: d.turn ?? 0, kind: d.kind ?? "?", description: d.description ?? "invalid action", coordinates: d.coordinates ?? [], action: {}, proposedAt: e.ts, frameBefore: latestFrameSeq };
          steps.set(d.step, s);
          t.steps.push(s);
        }
        s.executed = {
          ok: !!d.ok,
          error: d.error ?? null,
          duration_ms: d.duration_ms ?? 0,
          frame_seq: d.frame_seq ?? null,
          screen_changed: d.screen_changed ?? null,
          blocked: d.blocked,
          rejected: d.rejected,
          output: d.output ?? null,
        };
        break;
      }
      case "stuck.nudged": {
        const s = steps.get(d.step);
        if (s) s.nudged = true;
        note(e, "warn", "Stuck: screen unchanged", `"${d.description}" repeated ${d.repeats}×, nudging the model`);
        break;
      }
      case "session.paused":
        note(e, "info", "Paused", d.reason ? String(d.reason) : undefined);
        break;
      case "session.resumed":
        note(e, "info", "Resumed");
        break;
      case "user.instruction":
        note(e, "user", "Operator instruction", String(d.text ?? ""));
        break;
      case "manual.action":
        note(e, "user", `Operator: ${d.description}`, d.ok === false ? `failed: ${d.error}` : undefined, latestFrameSeq);
        break;
      case "operator.control":
        if (d.state === "taken") note(e, "user", "Operator took control");
        else note(e, "user", "Operator handed control back", d.summary ? String(d.summary) : "no input sent", latestFrameSeq);
        break;
      case "budget.changed": {
        const changes = (d.changes ?? {}) as Record<string, unknown>;
        const before = (d.before ?? {}) as Record<string, unknown>;
        const body = Object.keys(changes)
          .map((k) => `${k.replace(/_/g, " ")} ${String(before[k] ?? "–")} → ${String(changes[k])}`)
          .join(", ");
        note(e, "user", "Budget changed", body);
        if (d.budget) config = { ...(config ?? {}), ...(d.budget as Record<string, unknown>) };
        break;
      }
      case "error":
        note(e, "error", d.where ? `Error (${d.where})` : "Error", String(d.message ?? d.detail ?? ""));
        break;
      case "session.ended":
        ended = d;
        break;
      case "preview":
        lastPreviewSeq = e.seq;
        break;
      default:
        break;
    }
  }
  items.sort((a, b) => a.ts - b.ts || a.seq - b.seq);
  const flatSteps = [...steps.values()].sort((a, b) => a.step - b.step);
  return { items, steps: flatSteps, frames, frameBySeq, display, config, ended, pendingApproval, lastText, lastPreviewSeq };
}
