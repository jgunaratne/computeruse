/**
 * Agents tab: design flows of several agents on a canvas, wire their inputs and outputs, and run them.
 *
 * Layout: toolbar (flow picker, new-from-pattern, run/stop) over a palette | canvas | inspector row.
 * The page owns the flow document and autosaves it (debounced) after every edit; runs execute on the
 * server and the page long-polls the run's version, the same way the chat panel waits for replies.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Spinner, useAsync, useToast } from "../components/ui";
import { FlowCanvas } from "../flow/FlowCanvas";
import { FlowInspector, type InspectorTab } from "../flow/FlowInspector";
import { DRAG_MIME, addNode, duplicateNode, removeEdge, removeNode, updateNode, type Selection } from "../flow/graph";
import { navigate } from "../hooks/useRoute";
import { classNames, fmtRelative } from "../lib/format";
import type { Flow, FlowCatalog, FlowCategory, FlowNode, FlowNodeTypeSpec, FlowRun, FlowTemplateSummary } from "../types";

const CATEGORY_ORDER: { id: FlowCategory; label: string }[] = [
  { id: "start", label: "Start" },
  { id: "agent", label: "Agents" },
  { id: "flow", label: "Flow" },
  { id: "memory", label: "Memory" },
  { id: "end", label: "End" },
  { id: "note", label: "Notes" },
];

const RUN_TONE: Record<string, string> = { running: "live", completed: "ok", incomplete: "warn", failed: "bad", cancelled: "muted", interrupted: "warn" };

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function Palette({ catalog, onAdd }: { catalog: FlowCatalog; onAdd: (type: string) => void }) {
  return (
    <nav className="flow-palette" aria-label="Node palette">
      {CATEGORY_ORDER.map((cat) => {
        const items = catalog.node_types.filter((t) => t.category === cat.id);
        if (!items.length) return null;
        return (
          <section key={cat.id}>
            <h3>{cat.label}</h3>
            {items.map((t) => (
              <button
                key={t.type}
                type="button"
                className={classNames("flow-palette-item", `nt-${t.type}`)}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_MIME, t.type);
                  e.dataTransfer.effectAllowed = "copy";
                }}
                onClick={() => onAdd(t.type)}
                title={`${t.desc}\n\nDrag onto the canvas, or click to add.`}
              >
                <span className="flow-palette-title">{t.title}</span>
                <span className="flow-palette-desc">{t.desc}</span>
                {t.needs_antigravity && !catalog.antigravity.available && <span className="flow-palette-warn">needs Antigravity</span>}
              </button>
            ))}
          </section>
        );
      })}
    </nav>
  );
}

function TemplateCards({ templates, catalog, onCreate, busy }: { templates: FlowTemplateSummary[]; catalog: FlowCatalog; onCreate: (template?: string) => void; busy: boolean }) {
  return (
    <div className="flow-templates">
      {templates.map((t) => (
        <button key={t.id} type="button" className="craft-panel flow-template" onClick={() => onCreate(t.id)} disabled={busy}>
          <span className="flow-template-name">{t.name}</span>
          <span className="flow-template-desc">{t.description}</span>
          <span className="flow-template-tags">
            {t.tags.map((tag) => (
              <span key={tag} className="tag">
                {tag}
              </span>
            ))}
            {t.needs_antigravity && !catalog.antigravity.available && <span className="tag warn">needs Antigravity</span>}
          </span>
        </button>
      ))}
      <button type="button" className="craft-panel flow-template blank" onClick={() => onCreate()} disabled={busy}>
        <span className="flow-template-name">Blank canvas</span>
        <span className="flow-template-desc">Start empty and drag agents in from the palette.</span>
      </button>
    </div>
  );
}

export function AgentsPage({ flowId }: { flowId?: string }) {
  const toast = useToast();
  const catalog = useAsync(() => api.flowCatalog(), []);
  const config = useAsync(() => api.config(), []);
  const backends = useAsync(() => api.backends(), []);
  const demos = useAsync(() => api.demoTasks(), []);
  const flows = useAsync(() => api.flows(), []);
  const [flow, setFlow] = useState<Flow | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [tab, setTab] = useState<InspectorTab>("inspect");
  const [run, setRun] = useState<FlowRun | null>(null);
  const [saveState, setSaveState] = useState<"saved" | "dirty" | "saving" | "error">("saved");
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const specs = useMemo<Record<string, FlowNodeTypeSpec>>(() => Object.fromEntries((catalog.data?.node_types ?? []).map((t) => [t.type, t])), [catalog.data]);

  // -- the document, with debounced autosave --
  const flowRef = useRef<Flow | null>(null);
  const pending = useRef<Flow | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const revision = useRef(0);

  const saveNow = useCallback(async () => {
    window.clearTimeout(timer.current);
    const f = pending.current;
    if (!f) return;
    if (!f.name.trim()) {
      setSaveState("error");
      return;
    }
    pending.current = null;
    const rev = revision.current;
    setSaveState("saving");
    try {
      const saved = await api.saveFlow(f);
      if (revision.current === rev) setSaveState("saved");
      setFlow((cur) => (cur && cur.id === saved.id ? { ...cur, updated_at: saved.updated_at } : cur));
      if (flows.data?.find((x) => x.id === saved.id)?.name !== saved.name) flows.reload();
    } catch (e) {
      pending.current = pending.current ?? f;
      setSaveState("error");
      toast(`Could not save the flow: ${message(e)}`, "bad");
    }
  }, [flows, toast]);

  const update = useCallback(
    (next: Flow) => {
      revision.current++;
      flowRef.current = next;
      pending.current = next;
      setFlow(next);
      setSaveState("dirty");
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => void saveNow(), 600);
    },
    [saveNow],
  );

  // Flush a pending save when leaving the page or the tab.
  useEffect(() => {
    const flush = () => {
      const f = pending.current;
      if (f && f.name.trim()) {
        void fetch(`/api/flows/${f.id}`, { method: "PUT", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify(f) });
        pending.current = null;
      }
    };
    window.addEventListener("beforeunload", flush);
    return () => {
      window.removeEventListener("beforeunload", flush);
      void saveNow();
    };
  }, [saveNow]);

  // -- routing: /agents picks the latest flow; /agents/<id> loads it with its latest run --
  useEffect(() => {
    if (!flowId && flows.data?.length) navigate(`/agents/${flows.data[0].id}`, true);
  }, [flowId, flows.data]);

  useEffect(() => {
    void saveNow();
    setFlow(null);
    setRun(null);
    setSelection(null);
    setLoadError(null);
    flowRef.current = null;
    if (!flowId) return;
    let alive = true;
    api
      .flow(flowId)
      .then((f) => {
        if (!alive) return;
        flowRef.current = f;
        setFlow(f);
        setSaveState("saved");
      })
      .catch((e) => alive && setLoadError(e instanceof ApiError && e.status === 404 ? "This flow no longer exists." : message(e)));
    api
      .flowRuns(flowId)
      .then(async (runs) => {
        if (!alive || !runs.length) return;
        const latest = await api.flowRun(runs[0].id);
        if (alive) setRun(latest);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowId]);

  // -- follow a live run --
  const runId = run?.id;
  const live = run?.status === "running";
  useEffect(() => {
    if (!runId || !live) return;
    const ctrl = new AbortController();
    let alive = true;
    let version = run?.version ?? -1;
    (async () => {
      while (alive) {
        try {
          const next = await api.flowRun(runId, version, 20, ctrl.signal);
          if (!alive) return;
          version = next.version;
          setRun(next);
          if (next.status !== "running") {
            flows.reload();
            toast(next.status === "completed" ? "Run completed." : `Run ${next.status}: ${next.reason}`, next.status === "completed" ? undefined : "bad");
            return;
          }
        } catch {
          if (!alive) return;
          await new Promise((r) => setTimeout(r, 1500));
        }
      }
    })();
    return () => {
      alive = false;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, live]);

  // -- actions --
  const createFlow = async (template?: string) => {
    setBusy(true);
    setMenuOpen(false);
    try {
      await saveNow();
      const f = await api.createFlow(template ? { template } : {});
      flows.reload();
      navigate(`/agents/${f.id}`);
    } catch (e) {
      toast(message(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const deleteFlow = async () => {
    const f = flowRef.current;
    if (!f || !window.confirm(`Delete "${f.name}"? Its run history is kept on disk.`)) return;
    pending.current = null;
    try {
      await api.deleteFlow(f.id);
      const rest = (flows.data ?? []).filter((x) => x.id !== f.id);
      flows.reload();
      navigate(rest.length ? `/agents/${rest[0].id}` : "/agents", true);
    } catch (e) {
      toast(message(e), "bad");
    }
  };

  const startRun = async () => {
    const f = flowRef.current;
    if (!f) return;
    setBusy(true);
    try {
      await saveNow();
      const r = await api.startFlowRun(f.id);
      setRun(r);
    } catch (e) {
      toast(message(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const stopRun = async () => {
    if (!run) return;
    try {
      setRun(await api.cancelFlowRun(run.id));
    } catch (e) {
      toast(message(e), "bad");
    }
  };

  const onAdd = useCallback(
    (type: string, pos?: [number, number]) => {
      const f = flowRef.current;
      const spec = specs[type];
      if (!f || !spec) return;
      const at = pos ?? [(-f.viewport.x + 320) / f.viewport.zoom + (f.nodes.length % 5) * 24, (-f.viewport.y + 160) / f.viewport.zoom + (f.nodes.length % 5) * 24];
      const [next, id] = addNode(f, spec, at);
      update(next);
      setSelection({ kind: "node", id });
      setTab("inspect");
    },
    [specs, update],
  );

  const onDelete = useCallback(
    (sel: Selection) => {
      const f = flowRef.current;
      if (!f || !sel) return;
      update(sel.kind === "node" ? removeNode(f, sel.id) : removeEdge(f, sel.id));
      setSelection(null);
    },
    [update],
  );

  const onDuplicate = useCallback(
    (id: string) => {
      const f = flowRef.current;
      if (!f) return;
      const [next, copy] = duplicateNode(f, id);
      update(next);
      if (copy) setSelection({ kind: "node", id: copy });
    },
    [update],
  );

  const onToggleMute = useCallback(
    (id: string) => {
      const f = flowRef.current;
      const n = f?.nodes.find((x) => x.id === id);
      if (f && n) update(updateNode(f, id, { muted: !n.muted }));
    },
    [update],
  );

  const onChangeNode = useCallback(
    (id: string, patch: Partial<Pick<FlowNode, "label" | "config" | "muted">>) => {
      const f = flowRef.current;
      if (f) update(updateNode(f, id, patch));
    },
    [update],
  );

  const onSelect = useCallback((s: Selection) => setSelection(s), []);
  const onError = useCallback((m: string) => toast(m, "bad"), [toast]);

  // -- one-line summaries on the cards --
  const summaries = useMemo(() => {
    const out: Record<string, string> = {};
    if (!flow || !catalog.data) return out;
    const ag = catalog.data.antigravity;
    const agLabel = (id: unknown) => (id ? (ag.models.find((m) => m.id === id)?.label ?? String(id)) : "chat default");
    const cuLabel = (id: unknown) => (id ? (config.data?.models.find((m) => m.id === id)?.label ?? String(id)) : "default model");
    const beLabel = (id: unknown) => backends.data?.find((b) => b.id === id)?.label.split(/\s+[—-]\s+/)[0] ?? String(id || "?");
    const role = (id: unknown) => catalog.data!.roles.find((r) => r.value === id)?.label ?? String(id);
    for (const n of flow.nodes) {
      const c = n.config;
      if (n.type === "computer") out[n.id] = c.demo_task ? `scripted demo · ${String(c.demo_task).split("/").pop()}` : `${beLabel(c.backend)} · ${cuLabel(c.model)} · ≤${c.max_steps} steps`;
      else if (n.type === "model") out[n.id] = `${role(c.role)} · ${agLabel(c.model)}${c.memory === "fresh" ? " · fresh each turn" : ""}`;
      else if (n.type === "critic") out[n.id] = `≤${c.max_rounds} rounds · ${agLabel(c.model)}`;
    }
    return out;
  }, [flow, catalog.data, config.data, backends.data]);

  // -- render --
  if (catalog.error) return <div className="page"><div className="notice bad">Cannot load the flow editor: {catalog.error}</div></div>;
  if (!catalog.data || !flows.data) return <div className="page"><Spinner /></div>;

  if (!flowId && !flows.data.length) {
    return (
      <div className="page fade-in">
        <div className="page-head">
          <div>
            <h1>Agents</h1>
            <p className="sub">Wire several agents together — computer agents, model agents, critics, joins and a shared journal — and run them as one flow. Start from a pattern:</p>
          </div>
        </div>
        <TemplateCards templates={catalog.data.templates} catalog={catalog.data} onCreate={(t) => void createFlow(t)} busy={busy} />
      </div>
    );
  }

  const cat = catalog.data;
  return (
    <div className="flow-page">
      <header className="flow-toolbar">
        <h1 className="flow-toolbar-title">Agents</h1>
        <select
          className="select sm-select flow-picker"
          value={flowId ?? ""}
          onChange={(e) => navigate(`/agents/${e.target.value}`)}
          aria-label="Flow"
        >
          {flows.data.map((f) => (
            <option key={f.id} value={f.id}>
              {f.id === flow?.id ? flow.name || "(unnamed)" : f.name}
            </option>
          ))}
        </select>
        <div className="flow-new">
          <button
            type="button"
            className={classNames("craft-btn-compact btn sm", menuOpen && "is-active toggled")}
            onClick={() => setMenuOpen((v) => !v)}
            aria-expanded={menuOpen}
            disabled={busy}
          >
            New flow
          </button>
          {menuOpen && (
            <div className="craft-menu flow-new-menu" role="menu" onPointerLeave={() => setMenuOpen(false)}>
              {cat.templates.map((t) => (
                <button key={t.id} type="button" className="flow-new-item" role="menuitem" onClick={() => void createFlow(t.id)} title={t.description}>
                  <b>{t.name}</b>
                  <span>{t.description}</span>
                </button>
              ))}
              <div className="craft-menu-sep" />
              <button type="button" className="flow-new-item" role="menuitem" onClick={() => void createFlow()}>
                <b>Blank canvas</b>
                <span>Start empty.</span>
              </button>
            </div>
          )}
        </div>
        <button type="button" className="craft-btn-compact btn sm ghost danger" onClick={() => void deleteFlow()} disabled={!flow}>
          Delete
        </button>
        <span className="grow" />
        <span className={classNames("flow-save small", saveState)}>
          {saveState === "saving" ? "Saving…" : saveState === "dirty" ? "Unsaved" : saveState === "error" ? (flow?.name.trim() ? "Not saved" : "Name the flow to save") : flow ? `Saved ${fmtRelative(flow.updated_at)}` : ""}
        </span>
        {run && (
          <span className="flow-run-status" title={run.reason}>
            <span className={classNames("pill", RUN_TONE[run.status])}>{run.status}</span>
            <span className="small dim">
              {run.invocations}/{run.max_invocations} calls
            </span>
          </span>
        )}
        {live ? (
          <button type="button" className="craft-btn craft-btn-danger btn sm danger" onClick={() => void stopRun()}>
            Stop
          </button>
        ) : (
          <button type="button" className="craft-btn craft-btn-primary btn sm primary" onClick={() => void startRun()} disabled={!flow || busy}>
            {busy ? <Spinner /> : <span className="craft-icon craft-icon-sm" data-craft-icon="play" aria-hidden="true" />} Run flow
          </button>
        )}
      </header>

      <div className="flow-body">
        <Palette catalog={cat} onAdd={(t) => onAdd(t)} />
        <div className="flow-stage">
          {loadError ? (
            <div className="flow-empty"><strong>{loadError}</strong></div>
          ) : flow ? (
            <FlowCanvas
              flow={flow}
              specs={specs}
              run={run}
              selection={selection}
              summaries={summaries}
              onSelect={onSelect}
              onChange={update}
              onAdd={onAdd}
              onDelete={onDelete}
              onDuplicate={onDuplicate}
              onToggleMute={onToggleMute}
              onError={onError}
            />
          ) : (
            <div className="flow-empty"><Spinner /></div>
          )}
          {run && !live && (
            <button type="button" className="craft-btn-compact btn sm ghost flow-clear-run" onClick={() => setRun(null)} title="Hide the last run's results on the canvas">
              Clear run
            </button>
          )}
        </div>
        {flow && (
          <FlowInspector
            flow={flow}
            catalog={cat}
            specs={specs}
            selection={selection}
            run={run}
            tab={tab}
            computerModels={config.data?.models ?? []}
            defaultComputerModel={config.data?.model ?? ""}
            backends={backends.data ?? []}
            demoTasks={demos.data ?? []}
            onTab={setTab}
            onChangeNode={onChangeNode}
            onChangeFlow={(patch) => flowRef.current && update({ ...flowRef.current, ...patch })}
            onDelete={onDelete}
            onSelect={setSelection}
          />
        )}
      </div>
    </div>
  );
}
