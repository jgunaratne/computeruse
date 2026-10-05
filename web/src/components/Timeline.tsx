import { useEffect, useRef } from "react";
import type { Step, Timeline, Turn } from "../lib/timeline";
import { classNames, fmtClock, fmtMs, outcomeLabel } from "../lib/format";
import type { Outcome } from "../types";

export interface TimelineProps {
  timeline: Timeline;
  selected: number | null; // step index
  onSelect: (step: number | null) => void;
  follow: boolean;
}

export function TimelineView({ timeline, selected, onSelect, follow }: TimelineProps) {
  const ref = useRef<HTMLDivElement>(null);
  const count = timeline.items.length + timeline.steps.length;
  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [count, follow]);

  if (!timeline.items.length && !timeline.ended) {
    return (
      <div className="timeline">
        <div className="empty">Booting the computer…</div>
      </div>
    );
  }
  return (
    <div className="timeline" ref={ref}>
      {timeline.items.map((item) =>
        item.kind === "turn" ? (
          <TurnView key={`t${item.turn.index}`} turn={item.turn} selected={selected} onSelect={onSelect} />
        ) : (
          <div key={`n${item.seq}`} className={classNames("note", item.tone)}>
            <div className="t">{item.title}</div>
            {item.body && <div className="b">{item.body}</div>}
          </div>
        ),
      )}
      {timeline.ended && <Ended data={timeline.ended} />}
    </div>
  );
}

function TurnView({ turn, selected, onSelect }: { turn: Turn; selected: number | null; onSelect: (s: number | null) => void }) {
  const usage = turn.usage;
  return (
    <section className="turn">
      <div className="turn-head">
        <span>Turn {turn.index + 1}</span>
        <span className="dim">{fmtClock(turn.startedAt)}</span>
        {turn.latency_ms != null && <span className="dim">· model {fmtMs(turn.latency_ms)}</span>}
        {!!turn.retries && <span className="dim">· {turn.retries} retr{turn.retries === 1 ? "y" : "ies"}</span>}
        {usage && (
          <span className="dim" title="input / output tokens">
            · {usage.input_tokens}↑ {usage.output_tokens}↓
          </span>
        )}
      </div>
      {turn.texts.map((t, i) => (
        <p key={i} className="turn-text">
          {t}
        </p>
      ))}
      {turn.steps.map((s) => (
        <StepRow key={s.step} step={s} selected={selected === s.step} onSelect={() => onSelect(selected === s.step ? null : s.step)} />
      ))}
    </section>
  );
}

function StepRow({ step, selected, onSelect }: { step: Step; selected: boolean; onSelect: () => void }) {
  const ex = step.executed;
  const failed = ex && !ex.ok && !ex.blocked && !ex.rejected;
  const blocked = !!(ex?.blocked || ex?.rejected || step.guardrail?.decision === "block");
  const pending = !ex && !step.approval;
  return (
    <>
      <div
        className={classNames("step", selected && "selected", failed && "failed", blocked && "blocked")}
        onClick={onSelect}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && onSelect()}
        id={`step-${step.step}`}
      >
        <span className="n">{step.step + 1}</span>
        <span className="desc" title={step.description}>
          <span className="kind">{step.kind}</span>
          {step.description}
        </span>
        <span className="right">
          {step.nudged && <span title="screen did not change; model was nudged">⚠</span>}
          {step.approval && step.approval.approved === null && <span className="pill warn">approval</span>}
          {step.approval?.approved === true && <span title="approved by operator">✓ approved</span>}
          {step.approval?.approved === false && <span title="rejected by operator">✗ rejected</span>}
          {ex?.blocked && <span className="pill warn">blocked</span>}
          {failed && <span className="pill bad">error</span>}
          {ex && ex.ok && ex.screen_changed === false && step.kind !== "screenshot" && step.kind !== "wait" && step.kind !== "cursor_position" && (
            <span title="screenshot unchanged after this action">no change</span>
          )}
          {ex && <span>{fmtMs(ex.duration_ms)}</span>}
          {pending && <span className="spinner" />}
        </span>
      </div>
      {selected && (
        <div className="step-detail fade-in">
          <div className="kv">
            <b>action</b>
            <code>{JSON.stringify(step.action)}</code>
            {step.guardrail && (
              <>
                <b>guardrail</b>
                <span>
                  {step.guardrail.decision} · {step.guardrail.rule}: {step.guardrail.reason}
                </span>
              </>
            )}
            {step.approval && (
              <>
                <b>approval</b>
                <span>
                  {step.approval.reason} → {step.approval.approved === null ? "pending" : step.approval.approved ? "approved" : "rejected"}
                </span>
              </>
            )}
            {ex?.error && (
              <>
                <b>error</b>
                <span>{ex.error}</span>
              </>
            )}
            {ex?.output && (
              <>
                <b>output</b>
                <code>{ex.output}</code>
              </>
            )}
            {ex && (
              <>
                <b>result</b>
                <span>
                  {ex.ok ? "ok" : "failed"} in {fmtMs(ex.duration_ms)}
                  {ex.screen_changed != null && ` · screen ${ex.screen_changed ? "changed" : "unchanged"}`}
                  {ex.frame_seq != null && ` · frame #${ex.frame_seq}`}
                </span>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function Ended({ data }: { data: Record<string, unknown> }) {
  const outcome = String(data.outcome) as Outcome;
  const ok = outcome === "completed";
  return (
    <div className={classNames("ended", ok ? "ok" : "bad")}>
      <div className="row">
        <b>{outcomeLabel[outcome] ?? outcome}</b>
        <span className="dim small">
          {String(data.steps)} steps · {String(data.turns)} turns · {fmtMs(Number(data.duration_ms))}
          {data.cost_usd != null && ` · $${Number(data.cost_usd).toFixed(3)}`}
        </span>
      </div>
      {!!data.reason && !ok && <div className="small muted" style={{ marginTop: 4 }}>{String(data.reason)}</div>}
      {!!data.final_text && <div className="final">{String(data.final_text)}</div>}
    </div>
  );
}
