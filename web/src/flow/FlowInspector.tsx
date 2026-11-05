/**
 * Right-hand panel of the Agents tab: settings for the selected node (or wire, or the flow itself),
 * what that node did in the current run, and the run's message log.
 */

import { useEffect, useMemo, useRef } from "react";
import { Link } from "../hooks/useRoute";
import { classNames, fmtClock, fmtDuration } from "../lib/format";
import type { Backend, DemoTask, Flow, FlowCatalog, FlowFieldSpec, FlowNode, FlowNodeTypeSpec, FlowRun, FlowSignal, ModelInfo } from "../types";
import type { Selection } from "./graph";

export type InspectorTab = "inspect" | "log";

interface Props {
  flow: Flow;
  catalog: FlowCatalog;
  specs: Record<string, FlowNodeTypeSpec>;
  selection: Selection;
  run: FlowRun | null;
  tab: InspectorTab;
  computerModels: ModelInfo[];
  defaultComputerModel: string;
  backends: Backend[];
  demoTasks: DemoTask[];
  onTab: (tab: InspectorTab) => void;
  onChangeNode: (id: string, patch: Partial<Pick<FlowNode, "label" | "config" | "muted">>) => void;
  onChangeFlow: (patch: Partial<Pick<Flow, "name" | "settings">>) => void;
  onDelete: (s: Selection) => void;
  onSelect: (s: Selection) => void;
}

const RUN_TONE: Record<string, string> = { running: "live", completed: "ok", incomplete: "warn", failed: "bad", cancelled: "muted", interrupted: "warn" };

function FieldInput({ field, value, props, node }: { field: FlowFieldSpec; value: string | number; props: Props; node: FlowNode }) {
  const set = (v: string | number) => props.onChangeNode(node.id, { config: { [field.key]: v } });
  const id = `ff-${node.id}-${field.key}`;
  const str = String(value ?? "");
  let control: React.ReactNode;
  switch (field.kind) {
    case "textarea":
      control = (
        <textarea id={id} className="textarea" rows={field.key === "instructions" ? 7 : 4} value={str} placeholder={field.placeholder} onChange={(e) => set(e.target.value)} />
      );
      break;
    case "number":
      control = (
        <input
          id={id}
          className="input"
          type="number"
          min={field.min}
          max={field.max}
          value={str}
          onChange={(e) => set(e.target.value === "" ? "" : Number(e.target.value))}
          onBlur={(e) => {
            const n = Number(e.target.value);
            if (!Number.isFinite(n) || e.target.value === "") set(field.default);
            else set(Math.min(field.max ?? n, Math.max(field.min ?? n, Math.round(n))));
          }}
        />
      );
      break;
    case "select":
      control = (
        <select id={id} className="select" value={str} onChange={(e) => set(e.target.value)}>
          {(field.options ?? []).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      );
      break;
    case "role":
      control = (
        <select
          id={id}
          className="select"
          value={str}
          onChange={(e) => {
            const roles = props.catalog.roles;
            const prev = roles.find((r) => r.value === str)?.instructions ?? "";
            const next = roles.find((r) => r.value === e.target.value)?.instructions ?? "";
            const current = String(node.config.instructions ?? "");
            // Replace the instructions only if they are still the old role's preset (or empty).
            const config: Record<string, string | number> = { role: e.target.value };
            if (!current.trim() || current === prev) config.instructions = next;
            props.onChangeNode(node.id, { config });
          }}
        >
          {props.catalog.roles.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label}
            </option>
          ))}
        </select>
      );
      break;
    case "model": {
      const ag = props.catalog.antigravity;
      const def = ag.models.find((m) => m.id === ag.default_model);
      control = (
        <select id={id} className="select" value={str} onChange={(e) => set(e.target.value)} disabled={!ag.available}>
          <option value="">{ag.available ? `Chat default${def ? ` (${def.label})` : ""}` : "Antigravity unavailable"}</option>
          {ag.models.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.available}>
              {m.label}
              {!m.available ? " · quota" : !m.supports_images ? " · text only" : ""}
            </option>
          ))}
        </select>
      );
      break;
    }
    case "computer_model": {
      const scripted = Boolean(String(node.config.demo_task ?? ""));
      control = (
        <select id={id} className="select" value={str} onChange={(e) => set(e.target.value)} disabled={scripted}>
          <option value="">{scripted ? "Scripted (the demo replays its script)" : `Server default (${props.defaultComputerModel || "none"})`}</option>
          {props.computerModels.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.available} title={m.reason}>
              {m.label} · {m.provider}
              {!m.available ? " · unavailable" : ""}
            </option>
          ))}
        </select>
      );
      break;
    }
    case "backend": {
      const scripted = Boolean(String(node.config.demo_task ?? ""));
      control = (
        <select id={id} className="select" value={str} onChange={(e) => set(e.target.value)} disabled={scripted}>
          {props.backends.map((b) => (
            <option key={b.id} value={b.id} disabled={!b.available} title={b.reason ?? b.description}>
              {b.label}
              {!b.available ? " · unavailable" : ""}
            </option>
          ))}
          {!props.backends.some((b) => b.id === str) && str && <option value={str}>{str} (unknown)</option>}
        </select>
      );
      break;
    }
    case "demo_task":
      control = (
        <select id={id} className="select" value={str} onChange={(e) => set(e.target.value)}>
          <option value="">None — use a model</option>
          {props.demoTasks.map((t) => (
            <option key={t.id} value={t.id} title={t.instruction}>
              {t.suite}: {t.task_id}
            </option>
          ))}
        </select>
      );
      break;
    default:
      control = <input id={id} className="input" value={str} placeholder={field.placeholder} onChange={(e) => set(e.target.value)} />;
  }
  return (
    <div className="field">
      <label htmlFor={id} className="craft-pro-colon">
        {field.label}
      </label>
      {control}
      {field.doc && <span className="small">{field.doc}</span>}
    </div>
  );
}

function SignalView({ title, sig }: { title: string; sig: FlowSignal | null }) {
  if (!sig) return null;
  return (
    <details className="flow-signal" open={sig.chars < 400}>
      <summary>
        {title} <span className="dim">· {sig.kind}{sig.source_label ? ` from ${sig.source_label}` : ""} · {sig.chars} chars</span>
      </summary>
      <pre>{sig.text || "(empty)"}</pre>
    </details>
  );
}

function NodeRunState({ run, node }: { run: FlowRun; node: FlowNode }) {
  const s = run.nodes[node.id];
  if (!s || (s.status === "idle" && !s.runs)) return <p className="small dim">Not reached in this run.</p>;
  return (
    <div className="stack flow-run-state">
      <div className="row wrap">
        <span className={classNames("pill", s.status === "error" ? "bad" : s.status === "stopped" ? "warn" : s.status === "running" || s.status === "waiting" ? "live" : s.status === "done" ? "ok" : "")}>{s.status}</span>
        {s.runs > 0 && <span className="small dim">ran {s.runs}×</span>}
        {s.rounds > 0 && <span className="small dim">round {s.rounds}</span>}
        {s.verdict && <span className={classNames("flow-verdict", s.verdict)}>{s.verdict === "max_rounds" ? "stopped at max rounds" : s.verdict}</span>}
      </div>
      {s.detail && <p className="small muted">{s.detail}</p>}
      {s.error && <div className="notice bad">{s.error}</div>}
      {s.sessions.length > 0 && (
        <div className="row wrap small">
          {s.sessions.map((sid, i) => (
            <Link key={sid} to={`/sessions/${sid}`} className="flow-link">
              session {i + 1}: {sid.slice(0, 8)} ↗
            </Link>
          ))}
        </div>
      )}
      {s.frame && (
        <Link to={`/sessions/${s.frame.session_id}`} className="flow-thumb large">
          <img src={s.frame.url} alt="Latest screen" />
        </Link>
      )}
      {s.result != null ? <SignalView title="Result" sig={{ ...(s.last_input as FlowSignal), text: s.result, chars: s.result.length }} /> : <SignalView title="Last output" sig={s.last_output} />}
      <SignalView title="Last input" sig={s.last_input} />
    </div>
  );
}

function RunLog({ run, flow, specs, onSelect }: { run: FlowRun | null; flow: Flow; specs: Record<string, FlowNodeTypeSpec>; onSelect: (s: Selection) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const label = useMemo(() => {
    const m = new Map(flow.nodes.map((n) => [n.id, n.label || specs[n.type]?.title || n.id]));
    return (id?: string) => (id ? (m.get(id) ?? run?.nodes[id]?.label ?? id) : "");
  }, [flow.nodes, specs, run?.nodes]);
  const last = run?.log[run.log.length - 1]?.seq;
  useEffect(() => {
    const el = ref.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 120) el.scrollTop = el.scrollHeight;
  }, [last]);
  if (!run) return <p className="small dim">Run the flow to see every message that crosses a wire.</p>;
  return (
    <div className="flow-log" ref={ref}>
      {run.log.map((e) => (
        <div key={e.seq} className={classNames("flow-log-row", e.type, e.status === "error" && "bad", e.dropped && "dropped")}>
          <span className="flow-log-time mono">{fmtClock(e.ts)}</span>
          {e.type === "message" ? (
            <div className="flow-log-body">
              <div>
                <button type="button" className="flow-log-node" onClick={() => onSelect({ kind: "node", id: e.source! })}>
                  {label(e.source)}
                </button>
                <span className="dim">.{e.source_port} → </span>
                <button type="button" className="flow-log-node" onClick={() => onSelect({ kind: "node", id: e.target! })}>
                  {label(e.target)}
                </button>
                <span className="dim">.{e.target_port}</span> <span className="tag">{e.kind}</span>
                {e.dropped && <span className="tag">dropped: muted</span>}
              </div>
              {e.text && <div className="flow-log-text">{e.text}</div>}
            </div>
          ) : (
            <div className="flow-log-body">
              {e.node && (
                <button type="button" className="flow-log-node" onClick={() => onSelect({ kind: "node", id: e.node! })}>
                  {label(e.node)}
                </button>
              )}{" "}
              {e.type === "run" ? <strong>{e.text}</strong> : <span>{e.text}</span>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function FlowInspector(props: Props) {
  const { flow, specs, selection, run, tab, onTab, onChangeNode, onChangeFlow, onDelete, onSelect } = props;
  const node = selection?.kind === "node" ? flow.nodes.find((n) => n.id === selection.id) : undefined;
  const edge = selection?.kind === "edge" ? flow.edges.find((e) => e.id === selection.id) : undefined;
  const spec = node ? specs[node.type] : undefined;
  const nameOf = (id: string) => {
    const n = flow.nodes.find((x) => x.id === id);
    return n ? n.label || specs[n.type]?.title || id : id;
  };

  return (
    <aside className="flow-inspector" aria-label="Inspector">
      <div className="craft-tab-strip flow-tabs btn-group" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "inspect"}
          className={classNames("craft-tab btn sm", tab === "inspect" && "is-active toggled")}
          onClick={() => onTab("inspect")}
        >
          {node ? "Node" : edge ? "Wire" : "Flow"}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "log"}
          className={classNames("craft-tab btn sm", tab === "log" && "is-active toggled")}
          onClick={() => onTab("log")}
        >
          Run log{run ? ` · ${run.log.filter((e) => e.type === "message").length}` : ""}
        </button>
      </div>

      {tab === "log" ? (
        <RunLog run={run} flow={flow} specs={specs} onSelect={(s) => { onSelect(s); onTab("inspect"); }} />
      ) : node && spec ? (
        <div className="flow-inspector-body stack">
          <div>
            <h3>{spec.title}</h3>
            <p className="small muted">{spec.desc}</p>
          </div>
          <div className="field">
            <label htmlFor={`fl-${node.id}`} className="craft-pro-colon">
              Label
            </label>
            <input id={`fl-${node.id}`} className="input" value={node.label} placeholder={spec.title} maxLength={80} onChange={(e) => onChangeNode(node.id, { label: e.target.value })} />
          </div>
          {spec.fields.map((f) => (
            <FieldInput key={f.key} field={f} value={node.config[f.key] ?? f.default} props={props} node={node} />
          ))}
          {(spec.inputs.length > 0 || spec.outputs.length > 0) && (
            <div className="flow-port-list small">
              {spec.inputs.map((p) => (
                <div key={`in-${p.name}`}>
                  <i className={classNames("flow-port", `t-${p.type}`, !p.trigger && "latch")} /> <b>{p.label}</b> <span className="dim">in · {p.trigger ? "starts work" : "context only"}{p.doc ? ` — ${p.doc}` : ""}</span>
                </div>
              ))}
              {spec.outputs.map((p) => (
                <div key={`out-${p.name}`}>
                  <i className={classNames("flow-port", `t-${p.type}`)} /> <b>{p.label}</b> <span className="dim">out{p.doc ? ` — ${p.doc}` : ""}</span>
                </div>
              ))}
            </div>
          )}
          {run && spec.type !== "note" && (
            <section className="stack">
              <h3>In this run</h3>
              <NodeRunState run={run} node={node} />
            </section>
          )}
          <div className="row">
            <button type="button" className="craft-btn craft-btn-secondary btn sm" onClick={() => onChangeNode(node.id, { muted: !node.muted })}>
              {node.muted ? "Unmute" : "Mute"}
            </button>
            <span className="grow" />
            <button type="button" className="craft-btn craft-btn-danger btn sm danger" onClick={() => onDelete(selection)}>
              Delete node
            </button>
          </div>
        </div>
      ) : edge ? (
        <div className="flow-inspector-body stack">
          <h3>Wire</h3>
          <p>
            <b>{nameOf(edge.source)}</b>
            <span className="dim">.{edge.source_port}</span> → <b>{nameOf(edge.target)}</b>
            <span className="dim">.{edge.target_port}</span>
          </p>
          <p className="small muted">Every message sent on that output is delivered to that input.</p>
          <div className="row">
            <span className="grow" />
            <button type="button" className="craft-btn craft-btn-danger btn sm danger" onClick={() => onDelete(selection)}>
              Delete wire
            </button>
          </div>
        </div>
      ) : (
        <div className="flow-inspector-body stack">
          <div className="field">
            <label htmlFor="flow-name" className="craft-pro-colon">
              Flow name
            </label>
            <input id="flow-name" className="input" value={flow.name} maxLength={120} onChange={(e) => onChangeFlow({ name: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="flow-budget" className="craft-pro-colon">
              Agent calls per run
            </label>
            <input
              id="flow-budget"
              className="input"
              type="number"
              min={1}
              max={500}
              value={flow.settings.max_agent_calls}
              onChange={(e) => onChangeFlow({ settings: { max_agent_calls: Math.min(500, Math.max(1, Math.round(Number(e.target.value) || 1))) } })}
            />
            <span className="small">Every session and every model turn counts. A run that reaches the limit stops, so loops cannot run away.</span>
          </div>
          {run && (
            <section className="stack">
              <h3>Last run</h3>
              <div className="row wrap">
                <span className={classNames("pill", RUN_TONE[run.status])}>{run.status}</span>
                <span className="small dim">
                  {run.invocations}/{run.max_invocations} agent calls · {fmtDuration(((run.ended_at ?? Date.now() / 1000) - run.started_at) * 1000)}
                </span>
              </div>
              {run.reason && <p className="small muted">{run.reason}</p>}
              {run.results.map((r) => (
                <details key={r.node} className="flow-signal" open>
                  <summary>
                    {r.label}
                    {r.verdict && <span className={classNames("flow-verdict", r.verdict)}>{r.verdict === "max_rounds" ? "stopped at max rounds" : r.verdict}</span>}
                  </summary>
                  <pre>{r.text}</pre>
                </details>
              ))}
            </section>
          )}
          <section className="flow-help small muted stack">
            <h3>How it works</h3>
            <p>Drag agents from the palette and wire an output dot to an input dot of the same colour. A message on a filled input starts that node's work; a hollow input only keeps the latest message as context for its next turn.</p>
            <p>Loops are how critics send work back. They end when the critic passes, at its max rounds, or when the run's agent calls run out. Mute a node to drop everything going to and from it.</p>
          </section>
        </div>
      )}
    </aside>
  );
}
