/**
 * The patch-bay canvas: nodes you drag around, wires you drag from an output dot to an input dot.
 *
 * Interaction model (deliberately close to classic node editors):
 *  - drag the background to pan, wheel / pinch to zoom around the pointer, F to fit;
 *  - drag a node by its header; click to select; Delete removes the selection; right-click for more;
 *  - drag from a port to a port of the opposite direction and the same type — or onto a card, which
 *    picks its first compatible port; incompatible ports dim while you drag;
 *  - drop a palette item to add that node where it lands.
 * All edits go through the pure helpers in ./graph and up to the page via `onChange`.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNow } from "../components/ui";
import { classNames } from "../lib/format";
import type { Flow, FlowNodeTypeSpec, FlowRun, FlowViewport } from "../types";
import { NodeCard } from "./NodeCard";
import {
  DRAG_MIME,
  HEADER_H,
  NODE_W,
  clampZoom,
  connection,
  edgeKey,
  firstCompatiblePort,
  fitView,
  moveNode,
  portPoint,
  portType,
  snap,
  toGraph,
  wirePath,
  zoomAt,
  type PortRef,
  type Selection,
} from "./graph";

interface Props {
  flow: Flow;
  specs: Record<string, FlowNodeTypeSpec>;
  run: FlowRun | null;
  selection: Selection;
  summaries: Record<string, string>;
  /** Bump to ask the canvas to fit the flow into view. */
  fitRequest?: number;
  onSelect: (s: Selection) => void;
  onChange: (next: Flow) => void;
  onAdd: (type: string, pos: [number, number]) => void;
  onDelete: (s: Selection) => void;
  onDuplicate: (id: string) => void;
  onToggleMute: (id: string) => void;
  onError: (message: string) => void;
}

type Drag =
  | { kind: "pan"; x0: number; y0: number; vp: FlowViewport; moved: boolean }
  | { kind: "move"; id: string; x0: number; y0: number; pos: [number, number]; moved: boolean }
  | { kind: "connect"; from: PortRef; type: string; to: [number, number] };

interface Menu {
  x: number;
  y: number;
  sel: NonNullable<Selection>;
}

const isTyping = (t: EventTarget | null) => t instanceof HTMLElement && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName));

export function FlowCanvas(props: Props) {
  const { flow, specs, run, selection, summaries, fitRequest, onSelect, onChange, onAdd, onDelete, onDuplicate, onToggleMute, onError } = props;
  const canvasRef = useRef<HTMLDivElement>(null);
  const [vp, setVp] = useState<FlowViewport>(flow.viewport);
  const [connecting, setConnecting] = useState<Extract<Drag, { kind: "connect" }> | null>(null);
  const [menu, setMenu] = useState<Menu | null>(null);
  const drag = useRef<Drag | null>(null);
  const latest = useRef({ flow, vp, onChange });
  latest.current = { flow, vp, onChange };
  const commitTimer = useRef<number | undefined>(undefined);

  /** Keep the viewport in the document (so a reload comes back to the same view), debounced. */
  const commitViewport = useCallback((next: FlowViewport) => {
    window.clearTimeout(commitTimer.current);
    commitTimer.current = window.setTimeout(() => {
      const { flow: f, onChange: change } = latest.current;
      if (f.viewport.x !== next.x || f.viewport.y !== next.y || f.viewport.zoom !== next.zoom) change({ ...f, viewport: next });
    }, 400);
  }, []);
  useEffect(() => () => window.clearTimeout(commitTimer.current), []);

  const fit = useCallback(() => {
    const el = canvasRef.current;
    if (!el) return;
    const next = fitView(latest.current.flow.nodes, specs, el.clientWidth, el.clientHeight);
    setVp(next);
    commitViewport(next);
  }, [specs, commitViewport]);

  // A different flow brings its own viewport; a flow that was never framed is fitted to the canvas.
  useLayoutEffect(() => {
    const v = flow.viewport;
    if (v.x === 0 && v.y === 0 && v.zoom === 1 && flow.nodes.length) fit();
    else setVp(v);
    setMenu(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.id]);
  useEffect(() => {
    if (fitRequest) fit();
  }, [fitRequest, fit]);

  // Wheel / pinch zoom around the pointer. A native listener: React's wheel handlers are passive.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setMenu(null);
      const rect = el.getBoundingClientRect();
      const factor = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015));
      const next = zoomAt(latest.current.vp, rect, e.clientX, e.clientY, factor);
      setVp(next);
      commitViewport(next);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [commitViewport]);

  // Close the context menu on any outside press.
  useEffect(() => {
    if (!menu) return;
    const close = (e: PointerEvent) => {
      if (!(e.target instanceof Element && e.target.closest(".flow-menu"))) setMenu(null);
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [menu]);

  const graphPoint = useCallback((clientX: number, clientY: number): [number, number] => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return toGraph(latest.current.vp, rect, clientX, clientY);
  }, []);

  const finishConnect = useCallback(
    (d: Extract<Drag, { kind: "connect" }>, clientX: number, clientY: number) => {
      const hit = document.elementFromPoint(clientX, clientY);
      const f = latest.current.flow;
      let target: PortRef | null = null;
      const portEl = hit?.closest<HTMLElement>("[data-port]");
      if (portEl) {
        target = { node: portEl.dataset.node!, port: portEl.dataset.port!, dir: portEl.dataset.dir as PortRef["dir"] };
      } else {
        const card = hit?.closest<HTMLElement>("[data-node-id]");
        if (card && card.dataset.nodeId !== d.from.node) {
          target = firstCompatiblePort(f, specs, d.from, card.dataset.nodeId!);
          if (!target) onError("That node has no compatible port for this wire.");
        }
      }
      if (!target) return;
      const { edge, reason } = connection(f, specs, d.from, target);
      if (edge) {
        latest.current.onChange({ ...f, edges: [...f.edges, edge] });
        onSelect({ kind: "edge", id: edge.id });
      } else if (reason && reason !== "already wired") {
        onError(`Can't wire that: ${reason}.`);
      }
    },
    [specs, onError, onSelect],
  );

  // One pair of window listeners serves every drag (pan, move, connect).
  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      if (d.kind === "pan") {
        const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
        if (!d.moved && Math.hypot(dx, dy) < 3) return;
        d.moved = true;
        setVp({ ...d.vp, x: d.vp.x + dx, y: d.vp.y + dy });
      } else if (d.kind === "move") {
        const z = latest.current.vp.zoom;
        const dx = (e.clientX - d.x0) / z, dy = (e.clientY - d.y0) / z;
        if (!d.moved && Math.hypot(dx * z, dy * z) < 3) return;
        d.moved = true;
        const { flow: f, onChange: change } = latest.current;
        change(moveNode(f, d.id, [snap(d.pos[0] + dx), snap(d.pos[1] + dy)]));
      } else {
        d.to = graphPoint(e.clientX, e.clientY);
        setConnecting({ ...d });
      }
    };
    const up = (e: PointerEvent) => {
      const d = drag.current;
      drag.current = null;
      document.body.classList.remove("flow-dragging");
      if (!d) return;
      if (d.kind === "pan") {
        if (d.moved) commitViewport({ ...d.vp, x: d.vp.x + e.clientX - d.x0, y: d.vp.y + e.clientY - d.y0 });
        else onSelect(null);
      } else if (d.kind === "connect") {
        setConnecting(null);
        finishConnect(d, e.clientX, e.clientY);
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [graphPoint, commitViewport, finishConnect, onSelect]);

  const onBackgroundDown = (e: React.PointerEvent) => {
    if (e.button !== 0 && e.button !== 1) return;
    if (e.target instanceof Element && e.target.closest(".flow-node, .flow-wire-hit, .flow-overlay, .flow-menu")) return;
    canvasRef.current?.focus({ preventScroll: true });
    setMenu(null);
    drag.current = { kind: "pan", x0: e.clientX, y0: e.clientY, vp: latest.current.vp, moved: false };
    document.body.classList.add("flow-dragging");
  };

  const onHeadDown = useCallback((e: React.PointerEvent, id: string) => {
    if (e.button !== 0) return;
    const n = latest.current.flow.nodes.find((x) => x.id === id);
    if (!n) return;
    canvasRef.current?.focus({ preventScroll: true });
    setMenu(null);
    drag.current = { kind: "move", id, x0: e.clientX, y0: e.clientY, pos: n.pos, moved: false };
    document.body.classList.add("flow-dragging");
  }, []);

  const onPortDown = useCallback(
    (e: React.PointerEvent, ref: PortRef) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      e.preventDefault();
      const n = latest.current.flow.nodes.find((x) => x.id === ref.node);
      if (!n) return;
      const type = portType(specs[n.type], ref.port, ref.dir) ?? "text";
      const d: Extract<Drag, { kind: "connect" }> = { kind: "connect", from: ref, type, to: graphPoint(e.clientX, e.clientY) };
      drag.current = d;
      setConnecting(d);
      setMenu(null);
      document.body.classList.add("flow-dragging");
    },
    [specs, graphPoint],
  );

  const onSelectNode = useCallback((id: string) => onSelect({ kind: "node", id }), [onSelect]);
  const onNodeMenu = useCallback(
    (e: React.MouseEvent, id: string) => {
      e.preventDefault();
      onSelect({ kind: "node", id });
      setMenu({ x: e.clientX, y: e.clientY, sel: { kind: "node", id } });
    },
    [onSelect],
  );

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (isTyping(e.target)) return;
    if ((e.key === "Delete" || e.key === "Backspace") && selection) {
      e.preventDefault();
      onDelete(selection);
    } else if (e.key === "Escape") {
      setMenu(null);
      if (drag.current?.kind === "connect") {
        drag.current = null;
        setConnecting(null);
      } else onSelect(null);
    } else if (e.key === "f" || e.key === "F") {
      fit();
    }
  };

  const zoomBy = (factor: number) => {
    const el = canvasRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const next = zoomAt(vp, rect, rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
    setVp(next);
    commitViewport(next);
  };

  // -- run overlay: which wires carried messages, and which did so just now --
  const running = run?.status === "running";
  const now = useNow(running, 500);
  const traffic = useMemo(() => {
    const m = new Map<string, { seq: number; ts: number; dropped: boolean }>();
    for (const e of run?.log ?? []) {
      if (e.type !== "message" || !e.target) continue;
      m.set(`${e.source}.${e.source_port}>${e.target}.${e.target_port}`, { seq: e.seq, ts: e.ts, dropped: Boolean(e.dropped) });
    }
    return m;
  }, [run?.log]);

  const byId = useMemo(() => new Map(flow.nodes.map((n) => [n.id, n])), [flow.nodes]);

  const wires = flow.edges.map((e) => {
    const src = byId.get(e.source), dst = byId.get(e.target);
    if (!src || !dst || !specs[src.type] || !specs[dst.type]) return null;
    const a = portPoint(src, specs[src.type], e.source_port, "out");
    const b = portPoint(dst, specs[dst.type], e.target_port, "in");
    const type = portType(specs[src.type], e.source_port, "out") ?? "text";
    const t = traffic.get(edgeKey(e));
    const d = wirePath(a, b);
    const selected = selection?.kind === "edge" && selection.id === e.id;
    const fresh = running && t && now - t.ts * 1000 < 2500;
    return (
      <g key={e.id}>
        <path
          className="flow-wire-hit"
          d={d}
          onPointerDown={(ev) => {
            if (ev.button !== 0) return;
            ev.stopPropagation();
            canvasRef.current?.focus({ preventScroll: true });
            onSelect({ kind: "edge", id: e.id });
          }}
          onContextMenu={(ev) => {
            ev.preventDefault();
            onSelect({ kind: "edge", id: e.id });
            setMenu({ x: ev.clientX, y: ev.clientY, sel: { kind: "edge", id: e.id } });
          }}
        >
          <title>{`${src.label || specs[src.type].title}.${e.source_port} → ${dst.label || specs[dst.type].title}.${e.target_port}`}</title>
        </path>
        <path className={classNames("flow-wire", `t-${type}`, t && "used", t?.dropped && "dropped", selected && "selected", (src.muted || dst.muted) && "muted")} d={d} />
        {fresh && <path key={t!.seq} className={classNames("flow-wire-pulse", `t-${type}`)} d={d} />}
      </g>
    );
  });

  let ghost: React.ReactNode = null;
  if (connecting) {
    const n = byId.get(connecting.from.node);
    if (n && specs[n.type]) {
      const p = portPoint(n, specs[n.type], connecting.from.port, connecting.from.dir);
      const d = connecting.from.dir === "out" ? wirePath(p, connecting.to) : wirePath(connecting.to, p);
      ghost = <path className={classNames("flow-wire", "ghost", `t-${connecting.type}`)} d={d} />;
    }
  }

  const gridSize = 24 * vp.zoom;
  const menuNode = menu?.sel.kind === "node" ? byId.get(menu.sel.id) : undefined;

  return (
    <div
      ref={canvasRef}
      className={classNames("flow-canvas", connecting && "connecting")}
      data-connect-dir={connecting?.from.dir}
      data-connect-type={connecting?.type}
      tabIndex={0}
      aria-label="Flow canvas"
      style={{ backgroundSize: `${gridSize}px ${gridSize}px`, backgroundPosition: `${vp.x}px ${vp.y}px` }}
      onPointerDown={onBackgroundDown}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => {
        if (!(e.target instanceof Element && e.target.closest(".flow-node, .flow-wire-hit"))) e.preventDefault();
      }}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes(DRAG_MIME)) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }
      }}
      onDrop={(e) => {
        const type = e.dataTransfer.getData(DRAG_MIME);
        if (!type || !specs[type]) return;
        e.preventDefault();
        const [gx, gy] = graphPoint(e.clientX, e.clientY);
        onAdd(type, [gx - NODE_W / 2, gy - HEADER_H / 2]);
        canvasRef.current?.focus({ preventScroll: true });
      }}
    >
      <div className="flow-world" style={{ transform: `translate(${vp.x}px, ${vp.y}px) scale(${vp.zoom})` }}>
        <svg className="flow-wires" width="1" height="1" aria-hidden>
          {wires}
          {ghost}
        </svg>
        {flow.nodes.map((n) =>
          specs[n.type] ? (
            <NodeCard
              key={n.id}
              node={n}
              spec={specs[n.type]}
              state={run?.nodes[n.id]}
              selected={selection?.kind === "node" && selection.id === n.id}
              summary={summaries[n.id] ?? ""}
              onSelect={onSelectNode}
              onHeadDown={onHeadDown}
              onPortDown={onPortDown}
              onToggleMute={onToggleMute}
              onMenu={onNodeMenu}
            />
          ) : null,
        )}
      </div>

      {!flow.nodes.length && (
        <div className="flow-empty">
          <strong>Drag agents here from the palette</strong>
          <span>then wire an output dot to an input dot. Or start from a pattern in the toolbar.</span>
        </div>
      )}

      <div className="craft-panel flow-overlay flow-zoom" role="toolbar" aria-label="Zoom">
        <button type="button" className="craft-icon-btn craft-icon-btn-sm btn sm ghost icon" onClick={() => zoomBy(1 / 1.2)} title="Zoom out" aria-label="Zoom out">
          <span className="craft-icon craft-icon-sm" data-craft-icon="minus" aria-hidden="true" />
        </button>
        <button
          type="button"
          className="craft-btn-compact btn sm ghost flow-zoom-level"
          onClick={() => zoomBy(1 / vp.zoom)}
          title="Reset to 100%"
        >
          {Math.round(clampZoom(vp.zoom) * 100)}%
        </button>
        <button type="button" className="craft-icon-btn craft-icon-btn-sm btn sm ghost icon" onClick={() => zoomBy(1.2)} title="Zoom in" aria-label="Zoom in">
          <span className="craft-icon craft-icon-sm" data-craft-icon="plus" aria-hidden="true" />
        </button>
        <button type="button" className="craft-btn-compact btn sm ghost" onClick={fit} title="Fit the flow to the canvas (F)">
          Fit
        </button>
      </div>

      <div className="flow-overlay flow-legend" aria-hidden>
        <span><i className="flow-port t-text" /> text</span>
        <span><i className="flow-port t-screen" /> screen</span>
        <span><i className="flow-port t-text latch" /> context only</span>
      </div>

      {menu && (
        <div className="craft-menu flow-menu" role="menu" style={{ left: menu.x, top: menu.y }}>
          {menuNode ? (
            <>
              <div className="flow-menu-head ellipsis">{menuNode.label || specs[menuNode.type]?.title}</div>
              <button type="button" role="menuitem" className="craft-menu-item flow-menu-btn" onClick={() => { onToggleMute(menuNode.id); setMenu(null); }}>
                {menuNode.muted ? "Unmute" : "Mute"}
              </button>
              <button type="button" role="menuitem" className="craft-menu-item flow-menu-btn" onClick={() => { onDuplicate(menuNode.id); setMenu(null); }}>
                Duplicate
              </button>
              <div className="craft-menu-sep flow-menu-sep" />
              <button type="button" role="menuitem" className="craft-menu-item flow-menu-btn danger" onClick={() => { onDelete(menu.sel); setMenu(null); }}>
                Delete node
              </button>
            </>
          ) : (
            <button type="button" role="menuitem" className="craft-menu-item flow-menu-btn danger" onClick={() => { onDelete(menu.sel); setMenu(null); }}>
              Delete wire
            </button>
          )}
        </div>
      )}
    </div>
  );
}
