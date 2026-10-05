import { useState } from "react";
import { api } from "../api";
import { MAX_STEPS_CAP } from "../lib/limits";
import type { Timeline } from "../lib/timeline";
import type { InputAction, Session } from "../types";
import { useToast } from "./ui";

export interface ControlsProps {
  session: Session;
  timeline: Timeline;
  /** Operator holds control (server-side truth, from session metadata). */
  control: boolean;
  /** Control is held but the agent has not parked yet: inputs are not accepted. */
  controlPending: boolean;
  /** Low-latency input channel (websocket); returns false when disconnected. */
  sendInput: (action: InputAction, id?: number) => boolean;
  onChanged?: () => void;
}

export function Controls({ session, timeline, control, controlPending, sendInput }: ControlsProps) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [typeText, setTypeText] = useState("");
  const [keyText, setKeyText] = useState("");
  const st = session.status;
  const active = !session.terminal;
  const paused = st === "paused" || st === "awaiting_approval";

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    try {
      await fn();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "bad");
    } finally {
      setBusy(null);
    }
  };
  const command = (cmd: "pause" | "resume" | "step" | "cancel" | "take_control") => run(cmd, () => api.control(session.id, cmd));
  const release = (resume: boolean) => run("release", () => api.control(session.id, "release_control", { resume }));
  const approval = timeline.pendingApproval;

  // Quick inputs while in control go over the websocket (not persisted per keystroke); when merely
  // paused they use the recorded manual-action path so the timeline shows what was done.
  const quickInput = async (action: InputAction) => {
    if (control) {
      if (!sendInput(action, Date.now())) throw new Error("not connected to the session");
      return;
    }
    await api.manual(session.id, action);
  };

  if (!active) return null;
  return (
    <>
      {approval && (
        <div className="approval">
          <div className="title">Approval needed</div>
          <div className="small">
            <b>{approval.description}</b>
          </div>
          <div className="small muted" style={{ margin: "2px 0 8px" }}>
            {approval.reason}
          </div>
          <div className="row">
            <button className="btn primary sm" disabled={!!busy} onClick={() => run("approve", () => api.control(session.id, "approve", { approval_id: approval.approval_id }))}>
              Approve
            </button>
            <button className="btn danger sm" disabled={!!busy} onClick={() => run("reject", () => api.control(session.id, "reject", { approval_id: approval.approval_id }))}>
              Reject
            </button>
          </div>
        </div>
      )}
      <div className="controls">
        {control ? (
          <div className="row wrap">
            <span className="pill info" title="Your mouse and keyboard act on the computer; the agent is paused">
              {controlPending ? "taking control…" : "✋ you're in control"}
            </span>
            <span className="grow" />
            <button className="btn" disabled={!!busy} onClick={() => release(false)} title="Hand the computer back and keep the agent paused" id="btn-release">
              Hand back
            </button>
            <button className="btn primary" disabled={!!busy || st === "awaiting_approval"} onClick={() => release(true)} title="Hand back and let the agent continue; it is told what you did and sees the screen you left" id="btn-release-resume">
              ▶ Hand back &amp; resume
            </button>
            <button className="btn danger" disabled={!!busy} onClick={() => confirm("Cancel this session?") && command("cancel")} id="btn-cancel">
              Cancel
            </button>
          </div>
        ) : (
          <div className="row wrap">
            {st === "running" || st === "starting" ? (
              <button className="btn" disabled={!!busy || st === "starting"} onClick={() => command("pause")} id="btn-pause">
                ❚❚ Pause
              </button>
            ) : (
              <>
                <button className="btn primary" disabled={!!busy || st === "awaiting_approval"} onClick={() => command("resume")} id="btn-resume">
                  ▶ Resume
                </button>
                <button className="btn" disabled={!!busy || st !== "paused"} onClick={() => command("step")} title="Run exactly one more step, then pause again" id="btn-step">
                  Step ›
                </button>
              </>
            )}
            <button className="btn danger" disabled={!!busy} onClick={() => confirm("Cancel this session?") && command("cancel")} id="btn-cancel">
              Cancel
            </button>
            <span className="grow" />
            <button
              className="btn"
              disabled={!!busy || st === "starting" || st === "created"}
              title={paused ? "Drive the computer yourself with your mouse and keyboard; the agent is told what you did when you hand back" : "Pauses the agent after its current step, then your mouse and keyboard drive the computer"}
              onClick={() => command("take_control")}
              id="btn-take-control"
            >
              ✋ Take control
            </button>
          </div>
        )}
        {control && !controlPending && (
          <div className="control-hint" id="control-hint">
            Click the screen to focus it; your mouse, wheel and keyboard act on the computer. <span className="kbd">ctrl</span>/<span className="kbd">⌘</span>+<span className="kbd">v</span> pastes <i>your</i> clipboard,{" "}
            <span className="kbd">ctrl</span>+<span className="kbd">shift</span>+<span className="kbd">v</span> pastes the computer's. ⌘ is sent as ctrl. Hold <span className="kbd">esc</span> to hand back. Typed text is never logged.
          </div>
        )}
        {(control || paused) && (
          <div className="manual-pad">
            <span className="small muted">{control ? "Or send:" : "Send to the computer:"}</span>
            <input
              className="input"
              style={{ width: 200, height: 28 }}
              placeholder="text to type (e.g. a pasted token)…"
              value={typeText}
              onChange={(e) => setTypeText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && typeText) void run("type", async () => { await quickInput({ action: "type", text: typeText }); setTypeText(""); });
              }}
              id="input-type"
            />
            <input
              className="input"
              style={{ width: 130, height: 28 }}
              placeholder="key, e.g. ctrl+l"
              value={keyText}
              onChange={(e) => setKeyText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && keyText) void run("key", async () => { await quickInput({ action: "key", text: keyText }); setKeyText(""); });
              }}
              id="input-key"
            />
            {!control && <span className="small dim">Press Enter to send. These are recorded in the timeline; typed text is not logged.</span>}
          </div>
        )}
        <StepBudget session={session} timeline={timeline} busy={!!busy} run={run} />
        <form
          className="instruct"
          onSubmit={(e) => {
            e.preventDefault();
            if (!text.trim()) return;
            void run("instruct", async () => {
              await api.control(session.id, "instruct", { text: text.trim() });
              setText("");
            });
          }}
        >
          <input className="input" placeholder="Tell the agent something (delivered before its next turn)…" value={text} onChange={(e) => setText(e.target.value)} id="input-instruct" />
          <button className="btn" type="submit" disabled={!text.trim() || !!busy}>
            Send
          </button>
        </form>
      </div>
    </>
  );
}

/**
 * Executed steps against the step budget, with one-click raises. The budget lives in the running
 * loop's config, so a raise takes effect at the next check — before the session would have stopped.
 */
function StepBudget({
  session,
  timeline,
  busy,
  run,
}: {
  session: Session;
  timeline: Timeline;
  busy: boolean;
  run: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [custom, setCustom] = useState("");
  const maxSteps = session.metadata.budget?.max_steps ?? (timeline.config?.max_steps as number | undefined) ?? null;
  if (maxSteps == null) return null;
  const left = maxSteps - session.steps;
  const set = (n: number) =>
    run("budget", async () => {
      await api.control(session.id, "budget", { max_steps: n });
      setCustom("");
    });
  const custom_n = Number(custom);
  const customOk = custom !== "" && Number.isInteger(custom_n) && custom_n > session.steps && custom_n <= MAX_STEPS_CAP;
  return (
    <div className="row wrap" style={{ gap: 8, alignItems: "center" }} id="step-budget">
      <span className={`small ${left <= 5 ? "bad" : "muted"}`} title="Executed actions / step budget. Raise it any time before the session stops; the default comes from COMPUTERUSE_DEFAULT_MAX_STEPS.">
        Step budget <b>{session.steps}</b> / {maxSteps}
        {left <= 5 ? ` · ${left} left` : ""}
      </span>
      {[20, 100].map((n) => (
        <button key={n} type="button" className="btn sm" disabled={busy || maxSteps + n > MAX_STEPS_CAP} onClick={() => void set(maxSteps + n)} title={`Allow ${n} more steps`} id={`btn-steps-plus-${n}`}>
          +{n}
        </button>
      ))}
      <input
        className="input"
        type="number"
        style={{ width: 96, height: 28 }}
        min={session.steps + 1}
        max={MAX_STEPS_CAP}
        placeholder="set to…"
        value={custom}
        onChange={(e) => setCustom(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && customOk) void set(custom_n);
        }}
        aria-label="New step budget"
        title={`Type a new budget (more than ${session.steps}, at most ${MAX_STEPS_CAP}) and press Enter`}
        id="input-max-steps"
      />
      {custom !== "" && !customOk && <span className="small bad">must be between {session.steps + 1} and {MAX_STEPS_CAP}</span>}
    </div>
  );
}
