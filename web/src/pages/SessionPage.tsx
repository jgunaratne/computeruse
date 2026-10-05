import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Controls } from "../components/Controls";
import { ScreenView } from "../components/ScreenView";
import { TimelineView } from "../components/Timeline";
import { Pill, Spinner, StatusPill, VerdictPill, useNow, useToast } from "../components/ui";
import { Link } from "../hooks/useRoute";
import { useSessionStream } from "../hooks/useSessionStream";
import { fmtCost, fmtDuration, truncate } from "../lib/format";
import { buildTimeline } from "../lib/timeline";
import type { InputAction } from "../types";

export function SessionPage({ id }: { id: string }) {
  const stream = useSessionStream(id);
  const toast = useToast();
  const timeline = useMemo(() => buildTimeline(stream.events), [stream.events]);
  const [selected, setSelectedState] = useState<number | null>(() => {
    const v = new URLSearchParams(location.search).get("step");
    return v != null && v !== "" && !Number.isNaN(Number(v)) ? Number(v) - 1 : null;
  });
  const setSelected = useCallback((v: number | null | ((cur: number | null) => number | null)) => {
    setSelectedState((cur) => {
      const next = typeof v === "function" ? v(cur) : v;
      const url = new URL(location.href);
      if (next == null) url.searchParams.delete("step");
      else url.searchParams.set("step", String(next + 1));
      history.replaceState(null, "", url);
      return next;
    });
  }, []);
  const [showAfter, setShowAfter] = useState(false);
  const session = stream.session;
  const active = !!session && !session.terminal;
  const now = useNow(active);
  const steps = timeline.steps;
  const selectedStep = selected == null ? null : steps.find((s) => s.step === selected) ?? null;

  // Operator control is server-side state (session metadata), so every viewer agrees on it.
  const paused = session?.status === "paused" || session?.status === "awaiting_approval";
  const controlHeld = active && !!session?.metadata.operator_control?.active;
  const control = controlHeld && paused;
  const controlPending = controlHeld && !paused;

  // Keyboard scrubbing: ← → move through steps, Esc returns to live/latest (off while the operator types into the computer).
  useEffect(() => {
    if (controlHeld) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT" || (e.target as HTMLElement)?.tagName === "TEXTAREA") return;
      if (e.key === "Escape") setSelected(null);
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        setSelected((cur) => {
          if (!steps.length) return null;
          const idx = cur == null ? steps.length : steps.findIndex((s) => s.step === cur);
          const next = e.key === "ArrowLeft" ? Math.max(0, idx - 1) : idx + 1;
          return next >= steps.length ? null : steps[next].step;
        });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [steps, controlHeld, setSelected]);

  // Taking control always shows the live screen.
  useEffect(() => {
    if (controlHeld) setSelected(null);
  }, [controlHeld, setSelected]);

  // `?control=1` (sign-in sessions): take control as soon as the agent is parked.
  const autoControl = useRef(new URLSearchParams(location.search).get("control") === "1");
  useEffect(() => {
    if (!autoControl.current || !session || session.terminal || controlHeld || !paused) return;
    autoControl.current = false;
    const url = new URL(location.href);
    url.searchParams.delete("control");
    history.replaceState(null, "", url);
    api.control(id, "take_control").catch((e) => toast(e instanceof Error ? e.message : String(e), "bad"));
  }, [session, paused, controlHeld, id, toast]);

  const onInput = useCallback(
    (action: InputAction, inputId?: number) => {
      if (!stream.sendInput(action, inputId)) toast("Not connected to the session", "bad");
    },
    [stream.sendInput, toast],
  );
  const onReleaseControl = useCallback(() => {
    api.control(id, "release_control").catch((e) => toast(e instanceof Error ? e.message : String(e), "bad"));
  }, [id, toast]);

  // Failed inputs: one toast per burst, not one per dropped mouse move.
  const lastToastAt = useRef(0);
  useEffect(() => {
    const err = stream.inputError;
    if (!err || err.at - lastToastAt.current < 2000) return;
    lastToastAt.current = err.at;
    toast(`Input ${err.action ?? ""} failed: ${err.error ?? "unknown error"}`.replace("  ", " "), "bad");
  }, [stream.inputError, toast]);

  // Which image to show.
  const latestFrame = timeline.frames[timeline.frames.length - 1] ?? null;
  let src: string | null = null;
  let overlay = false;
  let liveNow = false;
  if (selectedStep) {
    const seq = showAfter ? selectedStep.executed?.frame_seq ?? selectedStep.frameBefore : selectedStep.frameBefore;
    src = (seq != null && timeline.frameBySeq.get(seq)?.url) || latestFrame?.url || null;
    overlay = !showAfter;
  } else if (active && stream.preview) {
    src = stream.preview;
    liveNow = true;
  } else {
    src = latestFrame?.url ?? null;
    liveNow = active;
  }

  const caption = selectedStep
    ? { label: `Step ${selectedStep.step + 1} · ${showAfter ? "result" : "decision"}`, text: selectedStep.description }
    : timeline.lastText
      ? { label: "Agent", text: truncate(timeline.lastText, 220) }
      : null;

  if (!session) {
    return (
      <div className="page">
        {stream.error ? <div className="notice bad">{stream.error}</div> : <div className="row"><Spinner /> <span className="muted">Loading session…</span></div>}
      </div>
    );
  }
  const elapsedMs = session.duration_ms ?? (session.started_at ? now - session.started_at * 1000 : null);
  const display = session.display_width && session.display_height ? { width: session.display_width, height: session.display_height } : timeline.display;
  const isolated = session.backend !== "desktop";
  const profile = session.metadata.computer?.profile ?? null;
  const maxSteps = session.metadata.budget?.max_steps ?? (timeline.config?.max_steps as number | undefined) ?? null;

  return (
    <div className="page wide fade-in">
      <div className="session-head">
        <div className="row wrap">
          <Link to="/" className="btn ghost sm">
            ← Sessions
          </Link>
          <StatusPill status={session.status} />
          <VerdictPill s={session} />
          {!stream.connected && active && <Pill tone="warn">reconnecting…</Pill>}
          {!isolated && <Pill tone="bad" title="This session drives your real desktop">NOT ISOLATED</Pill>}
          <span className="grow" />
          <span className="mono dim small">{session.id}</span>
        </div>
        <div className="task">{session.task}</div>
        <div className="meta">
          <span>
            backend <b>{session.backend}</b>
          </span>
          <span>
            model <b>{session.model}</b>
          </span>
          <span title="Executed actions / step budget (adjustable below while the session runs)">
            steps <b>{session.steps}</b>
            {maxSteps != null ? <span className="dim"> / {maxSteps}</span> : null}
          </span>
          <span>
            turns <b>{session.turns}</b>
          </span>
          <span>
            elapsed <b>{fmtDuration(elapsedMs)}</b>
          </span>
          <span>
            cost <b>{fmtCost(session.cost_usd)}</b>
          </span>
          {display && (
            <span>
              display <b>{display.width}×{display.height}</b>
            </span>
          )}
          {session.live_view_url && (
            <a className="pill info" href={session.live_view_url} target="_blank" rel="noreferrer">
              open live view ↗
            </a>
          )}
          {session.tags.length > 0 && (
            <span className="row" style={{ gap: 4 }}>
              {session.tags.map((t) => (
                <span key={t} className="tag">
                  {t}
                </span>
              ))}
            </span>
          )}
        </div>
      </div>

      <div className="session-layout">
        <div className="card screen-card">
          <div className="screen-toolbar">
            {selectedStep ? (
              <>
                <div className="btn-group">
                  <button className={`btn sm ${!showAfter ? "toggled" : ""}`} onClick={() => setShowAfter(false)} title="What the model saw when it chose this action">
                    Decision
                  </button>
                  <button className={`btn sm ${showAfter ? "toggled" : ""}`} onClick={() => setShowAfter(true)} title="Screen after the action ran">
                    Result
                  </button>
                </div>
                <span className="small muted">Step {selectedStep.step + 1} of {steps.length}</span>
                <span className="grow" />
                <span className="small dim">
                  <span className="kbd">←</span> <span className="kbd">→</span> scrub · <span className="kbd">esc</span> {active ? "live" : "latest"}
                </span>
                <button className="btn sm" onClick={() => setSelected(null)} id="btn-live">
                  {active ? "● Live" : "Latest"}
                </button>
              </>
            ) : (
              <>
                {liveNow ? (
                  <span className="live-badge">
                    <span className="pill live">LIVE</span>
                    <span className="dim small" style={{ fontWeight: 400 }}>
                      {stream.preview ? "streaming preview" : "waiting for frames"}
                    </span>
                  </span>
                ) : (
                  <span className="small muted">Final screen</span>
                )}
                <span className="grow" />
                {control && <Pill tone="info">you're in control</Pill>}
                {controlPending && <Pill tone="warn">pausing the agent…</Pill>}
                {profile && (
                  <span className="pill" title={profile.note + (profile.source ? ` (copy of ${profile.source})` : "")}>
                    {profile.persistent ? "🔐 profile: persistent" : profile.mode === "ephemeral" ? "profile: ephemeral" : "profile: copy (logins not saved)"}
                  </span>
                )}
                {steps.length > 0 && !controlHeld && (
                  <button className="btn sm" onClick={() => setSelected(steps[steps.length - 1].step)}>
                    Inspect last step
                  </button>
                )}
              </>
            )}
          </div>
          <ScreenView
            display={display}
            src={src}
            live={liveNow}
            step={selectedStep}
            showOverlay={overlay}
            control={control}
            controlPending={controlPending}
            onInput={onInput}
            onReleaseControl={onReleaseControl}
            caption={caption}
            placeholder={session.status === "starting" || session.status === "created" ? "Booting the computer…" : "No screenshot yet"}
          />
          {steps.length > 0 && (
            <div className="scrubber">
              <span className="small dim mono">1</span>
              <input
                type="range"
                min={0}
                max={steps.length}
                value={selected == null ? steps.length : steps.findIndex((s) => s.step === selected)}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setSelected(v >= steps.length ? null : steps[v].step);
                }}
                aria-label="Scrub through steps"
                disabled={controlHeld}
              />
              <span className="small dim mono">{active ? "live" : steps.length}</span>
            </div>
          )}
        </div>

        <div className="card">
          <Controls session={session} timeline={timeline} control={controlHeld} controlPending={controlPending} sendInput={stream.sendInput} />
          <TimelineView timeline={timeline} selected={selected} onSelect={setSelected} follow={selected == null && active} />
        </div>
      </div>
    </div>
  );
}
