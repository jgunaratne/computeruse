/**
 * The flow editor's graph layer: geometry and pure, immutable mutations on a `Flow`.
 *
 * Components never edit a flow in place: they call these helpers and hand the result to the page,
 * which autosaves it. Port positions are computed from fixed layout constants (not measured from the
 * DOM), so wires, hit-testing and the node cards always agree. Keep these constants in step with the
 * `.flow-node*` rules in styles.css.
 */

import type { Flow, FlowEdge, FlowNode, FlowNodeTypeSpec, FlowPortType, FlowViewport } from "../types";

export const NODE_W = 224;
export const NOTE_W = 208;
export const HEADER_H = 40;
export const PORT_TOP = 10;
export const PORT_ROW = 24;
export const GRID = 8;
export const MIN_ZOOM = 0.3;
export const MAX_ZOOM = 2;
/** MIME type a palette item carries while it is dragged onto the canvas. */
export const DRAG_MIME = "application/x-computeruse-node";

export type PortDir = "in" | "out";

export interface PortRef {
  node: string;
  port: string;
  dir: PortDir;
}

export type Selection = { kind: "node"; id: string } | { kind: "edge"; id: string } | null;

export const nodeWidth = (type: string) => (type === "note" ? NOTE_W : NODE_W);

export const portRows = (spec: FlowNodeTypeSpec) => Math.max(spec.inputs.length, spec.outputs.length);

/** Rough height, for fitting the view; the card's real height depends on its live content. */
export const estimateHeight = (spec: FlowNodeTypeSpec) => HEADER_H + (portRows(spec) ? PORT_TOP * 2 + portRows(spec) * PORT_ROW : 0) + 92;

export const snap = (v: number) => Math.round(v / GRID) * GRID;

export const edgeKey = (e: Pick<FlowEdge, "source" | "source_port" | "target" | "target_port">) =>
  `${e.source}.${e.source_port}>${e.target}.${e.target_port}`;

export function portType(spec: FlowNodeTypeSpec, port: string, dir: PortDir): FlowPortType | null {
  return (dir === "in" ? spec.inputs : spec.outputs).find((p) => p.name === port)?.type ?? null;
}

/** Centre of a port's dot, in graph coordinates. */
export function portPoint(node: FlowNode, spec: FlowNodeTypeSpec, port: string, dir: PortDir): [number, number] {
  const list = dir === "in" ? spec.inputs : spec.outputs;
  const i = Math.max(0, list.findIndex((p) => p.name === port));
  const x = dir === "in" ? node.pos[0] : node.pos[0] + nodeWidth(node.type);
  return [x, node.pos[1] + HEADER_H + PORT_TOP + i * PORT_ROW + PORT_ROW / 2];
}

/** A cubic wire that leaves an output to the right and enters an input from the left. Wires that run
 *  backwards (a loop back to an earlier node) swing out past both ends; when both ends sit on about the
 *  same row they also arc up over the cards in between, which would otherwise hide them. */
export function wirePath([x1, y1]: [number, number], [x2, y2]: [number, number]): string {
  if (x2 >= x1 + 40) {
    const dx = Math.max(50, (x2 - x1) * 0.5);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }
  const dx = Math.max(90, Math.abs(x2 - x1) * 0.35);
  const lift = Math.max(0, 1 - Math.abs(y2 - y1) / 200) * 150; // the arc peaks ~0.75 × lift above the ends
  return `M ${x1} ${y1} C ${x1 + dx} ${y1 - lift}, ${x2 - dx} ${y2 - lift}, ${x2} ${y2}`;
}

export function toGraph(vp: FlowViewport, rect: DOMRect, clientX: number, clientY: number): [number, number] {
  return [(clientX - rect.left - vp.x) / vp.zoom, (clientY - rect.top - vp.y) / vp.zoom];
}

export const clampZoom = (z: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));

/** Zoom by `factor` while keeping the graph point under (clientX, clientY) where it is on screen. */
export function zoomAt(vp: FlowViewport, rect: DOMRect, clientX: number, clientY: number, factor: number): FlowViewport {
  const zoom = clampZoom(vp.zoom * factor);
  const [gx, gy] = toGraph(vp, rect, clientX, clientY);
  return { zoom, x: clientX - rect.left - gx * zoom, y: clientY - rect.top - gy * zoom };
}

export function fitView(nodes: FlowNode[], specs: Record<string, FlowNodeTypeSpec>, width: number, height: number, pad = 40): FlowViewport {
  if (!nodes.length || width <= 0 || height <= 0) return { x: pad, y: pad, zoom: 1 };
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) {
    const spec = specs[n.type];
    x0 = Math.min(x0, n.pos[0]);
    y0 = Math.min(y0, n.pos[1]);
    x1 = Math.max(x1, n.pos[0] + nodeWidth(n.type));
    y1 = Math.max(y1, n.pos[1] + (spec ? estimateHeight(spec) : 160));
  }
  const zoom = clampZoom(Math.min((width - pad * 2) / (x1 - x0), (height - pad * 2) / (y1 - y0), 1.1));
  return { zoom, x: (width - (x1 - x0) * zoom) / 2 - x0 * zoom, y: (height - (y1 - y0) * zoom) / 2 - y0 * zoom };
}

export function defaultsFor(spec: FlowNodeTypeSpec): Record<string, string | number> {
  return Object.fromEntries(spec.fields.map((f) => [f.key, f.default]));
}

function uniqueId(flow: Flow, base: string): string {
  const taken = new Set(flow.nodes.map((n) => n.id));
  for (let i = 1; ; i++) {
    const id = `${base}_${i}`;
    if (!taken.has(id)) return id;
  }
}

export function addNode(flow: Flow, spec: FlowNodeTypeSpec, pos: [number, number]): [Flow, string] {
  const id = uniqueId(flow, spec.type);
  const node: FlowNode = { id, type: spec.type, label: "", pos: [snap(pos[0]), snap(pos[1])], config: defaultsFor(spec), muted: false };
  return [{ ...flow, nodes: [...flow.nodes, node] }, id];
}

export function duplicateNode(flow: Flow, id: string): [Flow, string | null] {
  const src = flow.nodes.find((n) => n.id === id);
  if (!src) return [flow, null];
  const copyId = uniqueId(flow, src.type);
  const copy: FlowNode = { ...src, id: copyId, label: src.label ? `${src.label} copy` : "", pos: [src.pos[0] + 32, src.pos[1] + 32], config: { ...src.config } };
  return [{ ...flow, nodes: [...flow.nodes, copy] }, copyId];
}

export function removeNode(flow: Flow, id: string): Flow {
  return { ...flow, nodes: flow.nodes.filter((n) => n.id !== id), edges: flow.edges.filter((e) => e.source !== id && e.target !== id) };
}

export function updateNode(flow: Flow, id: string, patch: Partial<Omit<FlowNode, "id" | "type">>): Flow {
  return { ...flow, nodes: flow.nodes.map((n) => (n.id === id ? { ...n, ...patch, config: patch.config ? { ...n.config, ...patch.config } : n.config } : n)) };
}

export function moveNode(flow: Flow, id: string, pos: [number, number]): Flow {
  return { ...flow, nodes: flow.nodes.map((n) => (n.id === id ? { ...n, pos } : n)) };
}

export function removeEdge(flow: Flow, id: string): Flow {
  return { ...flow, edges: flow.edges.filter((e) => e.id !== id) };
}

/** Whether `a` and `b` can be wired (in either order), and the edge that would result. */
export function connection(flow: Flow, specs: Record<string, FlowNodeTypeSpec>, a: PortRef, b: PortRef): { edge?: FlowEdge; reason?: string } {
  if (a.dir === b.dir) return { reason: a.dir === "out" ? "connect an output to an input" : "connect an input to an output" };
  const [from, to] = a.dir === "out" ? [a, b] : [b, a];
  if (from.node === to.node) return { reason: "a node cannot feed itself" };
  const src = flow.nodes.find((n) => n.id === from.node);
  const dst = flow.nodes.find((n) => n.id === to.node);
  if (!src || !dst) return { reason: "unknown node" };
  const outType = portType(specs[src.type], from.port, "out");
  const inType = portType(specs[dst.type], to.port, "in");
  if (!outType || !inType) return { reason: "unknown port" };
  if (outType !== inType) return { reason: `${outType} cannot go into ${inType}` };
  const edge: FlowEdge = { id: "", source: from.node, source_port: from.port, target: to.node, target_port: to.port };
  edge.id = edgeKey(edge);
  if (flow.edges.some((e) => e.id === edge.id)) return { reason: "already wired" };
  return { edge };
}

export function connect(flow: Flow, edge: FlowEdge): Flow {
  return { ...flow, edges: [...flow.edges, edge] };
}

/** The first port on `node` that `from` could be wired to (dropping a wire onto a card, not a dot). */
export function firstCompatiblePort(flow: Flow, specs: Record<string, FlowNodeTypeSpec>, from: PortRef, node: string): PortRef | null {
  const n = flow.nodes.find((x) => x.id === node);
  if (!n) return null;
  const spec = specs[n.type];
  const dir: PortDir = from.dir === "out" ? "in" : "out";
  const ports = [...(dir === "in" ? spec.inputs : spec.outputs)].sort((p, q) => Number(q.trigger) - Number(p.trigger));
  for (const p of ports) {
    const ref: PortRef = { node, port: p.name, dir };
    if (connection(flow, specs, from, ref).edge) return ref;
  }
  return null;
}
