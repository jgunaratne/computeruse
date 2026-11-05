/**
 * One node on the canvas: a header (drag handle, status, mute), typed port dots on the left and right
 * edges, and a body that shows the node's settings at rest and its live state during a run.
 */

import { memo, useEffect, useRef, useState } from "react";
import { Link } from "../hooks/useRoute";
import { classNames } from "../lib/format";
import type { FlowNode, FlowNodeState, FlowNodeTypeSpec, FlowPortSpec } from "../types";
import { HEADER_H, PORT_ROW, PORT_TOP, nodeWidth, portRows, type PortRef } from "./graph";

const STATUS_LABEL: Record<string, string> = {
  idle: "idle",
  waiting: "queued",
  running: "running",
  done: "done",
  stopped: "stopped",
  error: "error",
  muted: "muted",
};

const VERDICT_LABEL: Record<string, string> = { pass: "passed", revise: "revise", max_rounds: "stopped at max rounds" };

export interface NodeCardProps {
  node: FlowNode;
  spec: FlowNodeTypeSpec;
  state?: FlowNodeState;
  selected: boolean;
  summary: string;
  onSelect: (id: string) => void;
  onHeadDown: (e: React.PointerEvent, id: string) => void;
  onPortDown: (e: React.PointerEvent, ref: PortRef) => void;
  onToggleMute: (id: string) => void;
  onMenu: (e: React.MouseEvent, id: string) => void;
}

function Port({ node, port, dir, onDown }: { node: string; port: FlowPortSpec; dir: "in" | "out"; onDown: NodeCardProps["onPortDown"] }) {
  const title =
    dir === "out"
      ? `${port.label} (${port.type}) — drag to an input${port.doc ? `\n${port.doc}` : ""}`
      : `${port.label} (${port.type}, ${port.trigger ? "starts work" : "context only"})${port.doc ? `\n${port.doc}` : ""}`;
  return (
    <div className={classNames("flow-port-row", dir)}>
      <span
        className={classNames("flow-port", `t-${port.type}`, !port.trigger && dir === "in" && "latch")}
        data-port={port.name}
        data-dir={dir}
        data-type={port.type}
        data-node={node}
        title={title}
        aria-label={`${dir === "in" ? "input" : "output"} ${port.label}`}
        onPointerDown={(e) => onDown(e, { node, port: port.name, dir })}
      />
      <span className="flow-port-label">{port.label}</span>
    </div>
  );
}

function Thumb({ state }: { state: FlowNodeState }) {
  if (!state.frame) return null;
  return (
    <Link to={`/sessions/${state.frame.session_id}`} className="flow-thumb" title="Open the session" onPointerDown={(e) => e.stopPropagation()}>
      <img src={state.frame.url} alt="Latest screen" loading="lazy" draggable={false} />
    </Link>
  );
}

function Body({ node, spec, state, summary }: { node: FlowNode; spec: FlowNodeTypeSpec; state?: FlowNodeState; summary: string }) {
  const cfg = node.config;
  const text = (v: unknown) => (typeof v === "string" ? v : String(v ?? ""));
  const live = state && state.status !== "idle" && state.status !== "muted";
  switch (spec.type) {
    case "goal":
      return text(cfg.text) ? <p className="flow-node-text clamp-3">{text(cfg.text)}</p> : <p className="flow-node-text dim">Write the request in the inspector.</p>;
    case "note":
      return <p className="flow-node-text note-text">{text(cfg.text) || "Empty note"}</p>;
    case "computer":
      return (
        <>
          <p className="flow-node-meta">{summary}</p>
          {live && state?.session_id && (
            <p className="flow-node-meta">
              <Link to={`/sessions/${state.session_id}`} className="flow-link" onPointerDown={(e) => e.stopPropagation()}>
                session {state.session_id.slice(0, 8)} ↗
              </Link>
              {state.sessions.length > 1 && <span className="dim"> · {state.sessions.length} sessions</span>}
            </p>
          )}
          {state && <Thumb state={state} />}
          {live && state?.last_output && <p className="flow-node-text clamp-3">{state.last_output.text}</p>}
        </>
      );
    case "model":
      return (
        <>
          <p className="flow-node-meta">{summary}</p>
          {live && state?.last_output && <p className="flow-node-text clamp-4">{state.last_output.text}</p>}
        </>
      );
    case "critic":
      return (
        <>
          {!live && <p className="flow-node-text clamp-2 dim">{text(cfg.rubric)}</p>}
          <p className="flow-node-meta">
            {live ? `round ${state!.rounds} of ${cfg.max_rounds}` : summary}
            {state?.verdict && <span className={classNames("flow-verdict", state.verdict)}>{VERDICT_LABEL[state.verdict]}</span>}
          </p>
          {live && state?.verdict === "revise" && state.last_output && <p className="flow-node-text clamp-3">{state.last_output.text.replace(/^Round \d+ was not accepted\.\s*/, "")}</p>}
        </>
      );
    case "join":
      return <p className="flow-node-meta">{live ? state!.detail : "Waits for every wire, then sends them on together."}</p>;
    case "journal":
      return (
        <>
          <p className="flow-node-meta">{live ? `${state!.entries} ${state!.entries === 1 ? "entry" : "entries"} · journal.md` : "Appends every message; feeds agents as context."}</p>
          {live && state?.last_input && <p className="flow-node-text clamp-2">{state.last_input.source_label}: {state.last_input.text}</p>}
        </>
      );
    case "output":
      return state?.result != null ? (
        <>
          {state.verdict && <p className="flow-node-meta"><span className={classNames("flow-verdict", state.verdict)}>{VERDICT_LABEL[state.verdict]}</span></p>}
          <p className="flow-node-text clamp-6">{state.result}</p>
          <Thumb state={state} />
        </>
      ) : (
        <p className="flow-node-text dim">{live ? "Waiting for a result…" : "The run completes when every output has a result."}</p>
      );
    default:
      return null;
  }
}

function NodeCardImpl({ node, spec, state, selected, summary, onSelect, onHeadDown, onPortDown, onToggleMute, onMenu }: NodeCardProps) {
  const rows = portRows(spec);
  const status = node.muted ? "muted" : (state?.status ?? "idle");
  const title = node.label.trim() || spec.title;

  // Pulse when the node starts or finishes work during a run (not when a finished run is first shown).
  const key = state ? `${state.runs}:${state.status}:${state.entries}:${state.received}` : "";
  const prev = useRef<string | null>(null);
  const [pulse, setPulse] = useState(0);
  useEffect(() => {
    if (prev.current !== null && key && prev.current !== key) setPulse((p) => p + 1);
    prev.current = key;
  }, [key]);

  return (
    <div
      className={classNames("flow-node", `nt-${spec.type}`, `st-${status}`, selected && "selected")}
      style={{ left: node.pos[0], top: node.pos[1], width: nodeWidth(node.type) }}
      data-node-id={node.id}
      onPointerDown={(e) => {
        if (e.button === 0) onSelect(node.id);
      }}
      onContextMenu={(e) => onMenu(e, node.id)}
    >
      {pulse > 0 && <span key={pulse} className="flow-node-pulse" aria-hidden />}
      <div
        className="flow-node-head"
        style={{ height: HEADER_H }}
        onPointerDown={(e) => onHeadDown(e, node.id)}
        title={title === spec.title ? spec.desc : `${spec.title}: ${spec.desc}`}
      >
        <span className={classNames("flow-status-dot", `st-${status}`)} title={STATUS_LABEL[status]} />
        <span className="flow-node-title ellipsis">{title}</span>
        {spec.type !== "note" && (
          <button
            type="button"
            className={classNames("flow-mute", node.muted && "on")}
            title={node.muted ? "Unmute: let messages through again" : "Mute: drop every message to and from this node"}
            aria-pressed={node.muted}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              onToggleMute(node.id);
            }}
          >
            M
          </button>
        )}
      </div>
      {rows > 0 && (
        <div className="flow-ports" style={{ height: rows * PORT_ROW + PORT_TOP * 2, paddingTop: PORT_TOP }}>
          <div className="flow-ports-col in">
            {spec.inputs.map((p) => (
              <Port key={p.name} node={node.id} port={p} dir="in" onDown={onPortDown} />
            ))}
          </div>
          <div className="flow-ports-col out">
            {spec.outputs.map((p) => (
              <Port key={p.name} node={node.id} port={p} dir="out" onDown={onPortDown} />
            ))}
          </div>
        </div>
      )}
      <div className="flow-node-body">
        <Body node={node} spec={spec} state={node.muted ? undefined : state} summary={summary} />
        {state?.error && !node.muted && <p className="flow-node-error clamp-3">{state.error}</p>}
        {state?.detail && !node.muted && status !== "idle" && spec.type !== "join" && spec.type !== "journal" && (
          <p className="flow-node-detail ellipsis" title={state.detail}>
            {state.detail}
            {state.queued > 0 ? ` · ${state.queued} queued` : ""}
          </p>
        )}
      </div>
    </div>
  );
}

export const NodeCard = memo(NodeCardImpl);
