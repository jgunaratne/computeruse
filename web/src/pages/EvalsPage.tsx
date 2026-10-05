import { useState } from "react";
import { api } from "../api";
import { ModelSelect, discoverySummary, useModels } from "../components/ModelSelect";
import { Empty, Pill, Spinner, useAsync, useToast } from "../components/ui";
import { Link, navigate } from "../hooks/useRoute";
import { fmtCost, fmtPct, fmtRelative } from "../lib/format";
import type { Config, EvalRun, Suite } from "../types";

export function EvalsPage({ runId }: { runId?: string }) {
  const suites = useAsync(() => api.suites(), []);
  const config = useAsync(() => api.config(), []);
  const runs = useAsync(() => api.evalRuns(), [], { pollMs: 5000 });
  return (
    <div className="page fade-in">
      <div className="page-head">
        <div>
          <h1>Evals</h1>
          <p className="sub">Reproducible task suites with programmatic verifiers. Every eval session is a normal session you can open and replay.</p>
        </div>
      </div>
      {runId ? (
        <RunDetail runId={runId} />
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          <div className="card">
            <div className="card-head">
              <h2>Suites</h2>
            </div>
            {suites.data && config.data ? <SuiteList suites={suites.data} config={config.data} onStarted={() => runs.reload()} /> : <div className="card-body"><Spinner /></div>}
          </div>
          <div className="card">
            <div className="card-head">
              <h2>Runs</h2>
            </div>
            {!runs.data ? (
              <div className="card-body">
                <Spinner />
              </div>
            ) : runs.data.length === 0 ? (
              <Empty>No eval runs yet.</Empty>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Suite</th>
                    <th>Computer</th>
                    <th>Model</th>
                    <th>Status</th>
                    <th className="num">Pass rate</th>
                    <th className="num">False completions</th>
                    <th className="num">Cost</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.data.map((r) => (
                    <tr key={r.id} className="clickable" onClick={() => navigate(`/evals/${r.id}`)}>
                      <td className="mono small">{r.id}</td>
                      <td>{r.suite}</td>
                      <td>{r.backend}</td>
                      <td className="mono small">{r.model}</td>
                      <td>
                        <RunStatus r={r} />
                      </td>
                      <td className="num">{r.summary.pass_rate != null ? `${fmtPct(r.summary.pass_rate)} (${r.summary.passed}/${r.summary.runs})` : "–"}</td>
                      <td className="num">{r.summary.false_completions ?? "–"}</td>
                      <td className="num">{r.summary.total_cost_usd != null ? fmtCost(r.summary.total_cost_usd) : "–"}</td>
                      <td className="dim small">{fmtRelative(r.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function RunStatus({ r }: { r: EvalRun }) {
  if (r.status === "running") return <Pill tone="live">running</Pill>;
  if (r.status === "failed") return <Pill tone="bad">failed</Pill>;
  const pr = r.summary.pass_rate ?? 0;
  return <Pill tone={pr >= 0.9 ? "ok" : pr >= 0.6 ? "warn" : "bad"}>finished</Pill>;
}

function SuiteList({ suites, config, onStarted }: { suites: Suite[]; config: Config; onStarted: () => void }) {
  const live = useModels({ models: config.models, discovery: config.models_discovery, defaultModel: config.model });
  return (
    <>
      <div className="row small muted" style={{ padding: "0 16px 8px", gap: 8 }}>
        <span className="grow">Models: {live.error ?? discoverySummary(live.discovery)}</span>
        <button className="btn sm ghost" onClick={() => void live.refresh()} disabled={live.refreshing || live.discovery.status === "off"} title="Re-verify which models the server's credentials can call">
          {live.refreshing ? <span className="spinner" aria-hidden /> : "↻"} {live.refreshing ? "Verifying…" : "Refresh models"}
        </button>
      </div>
      {suites.map((s) => (
        <SuiteRow key={s.name} suite={s} config={config} live={live} onStarted={onStarted} />
      ))}
    </>
  );
}

function SuiteRow({ suite, config, live, onStarted }: { suite: Suite; config: Config; live: ReturnType<typeof useModels>; onStarted: () => void }) {
  const toast = useToast();
  const [model, setModel] = useState(config.models_available ? config.model : "scripted");
  const [repeats, setRepeats] = useState(1);
  const [concurrency, setConcurrency] = useState(suite.backend === "desktop" ? 1 : 2);
  const [busy, setBusy] = useState(false);
  const scriptable = suite.tasks.filter((t) => t.has_script).length;
  const start = async () => {
    setBusy(true);
    try {
      const r = await api.startEval({ suite: suite.name, model, repeats, concurrency });
      toast(`Eval run ${r.run_id} started (${r.tasks} sessions)`);
      onStarted();
      navigate(`/evals/${r.run_id}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "bad");
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="suite" style={{ borderBottom: "1px solid var(--line)" }}>
      <summary>
        <b>{suite.name}</b>
        <span className="tag">{suite.backend}</span>
        <span className="dim small">
          {suite.tasks.length} tasks · {scriptable} with reference scripts
        </span>
        <span className="grow" />
        <span className="row" onClick={(e) => e.preventDefault()}>
          <ModelSelect
            id={`model-${suite.name}`}
            compact
            allowCustom={false}
            value={model}
            onChange={setModel}
            models={live.models}
            discovery={live.discovery}
            extraOptions={[{ value: "scripted", label: "scripted (reference)" }]}
          />
          <input className="input" type="number" min={1} max={20} style={{ width: 64, height: 28 }} value={repeats} onChange={(e) => setRepeats(Number(e.target.value))} title="repeats per task" aria-label="Repeats" />
          <input className="input" type="number" min={1} max={8} style={{ width: 64, height: 28 }} value={concurrency} onChange={(e) => setConcurrency(Number(e.target.value))} title="concurrent sessions" aria-label="Concurrency" />
          <button className="btn primary sm" disabled={busy} onClick={start}>
            Run
          </button>
        </span>
      </summary>
      <div className="card-body" style={{ paddingTop: 0 }}>
        <p className="small muted" style={{ marginBottom: 10 }}>
          {suite.description}
        </p>
        <table className="table">
          <thead>
            <tr>
              <th>Task</th>
              <th>Instruction</th>
              <th>Verifier</th>
              <th>Tags</th>
            </tr>
          </thead>
          <tbody>
            {suite.tasks.map((t) => (
              <tr key={t.id}>
                <td className="mono small">{t.id}</td>
                <td>{t.instruction}</td>
                <td className="mono small dim">{String(t.checker.type)}</td>
                <td>
                  <span className="row wrap" style={{ gap: 4 }}>
                    {t.tags.map((x) => (
                      <span key={x} className="tag">
                        {x}
                      </span>
                    ))}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const run = useAsync(() => api.evalRun(runId), [runId], { pollMs: 4000 });
  const r = run.data;
  if (!r) return run.error ? <div className="notice bad">{run.error}</div> : <Spinner />;
  const s = r.summary;
  const sessions = r.sessions ?? [];
  const done = sessions.filter((x) => x.terminal).length;
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="row wrap">
        <Link to="/evals" className="btn ghost sm">
          ← Runs
        </Link>
        <RunStatus r={r} />
        <span className="mono dim small">{r.id}</span>
        <span className="muted">
          {r.suite} on {r.backend} with <span className="mono">{r.model}</span>
        </span>
      </div>
      <div className="stats">
        <div className="stat">
          <div className="label">Pass rate</div>
          <div className="value">{s.pass_rate != null ? fmtPct(s.pass_rate, 1) : r.status === "running" ? `${done}/${sessions.length || "?"} done` : "–"}</div>
          {s.runs != null && <div className="hint">{s.passed}/{s.runs} sessions passed</div>}
        </div>
        <div className="stat">
          <div className="label">False completions</div>
          <div className="value" style={s.false_completions ? { color: "var(--bad)" } : undefined}>{s.false_completions ?? "–"}</div>
          <div className="hint">claimed done, verifier failed</div>
        </div>
        <div className="stat">
          <div className="label">Mean steps</div>
          <div className="value">{s.mean_steps ?? "–"}</div>
        </div>
        <div className="stat">
          <div className="label">Cost / wall</div>
          <div className="value">{s.total_cost_usd != null ? fmtCost(s.total_cost_usd) : "–"}</div>
          {s.wall_s != null && <div className="hint">{s.wall_s}s wall clock</div>}
        </div>
      </div>
      {s.error && <div className="notice bad">{s.error}</div>}
      <div className="card">
        <div className="card-head">
          <h2>Sessions</h2>
          <span className="small dim">click to open the trace</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Task</th>
              <th>Verdict</th>
              <th>Outcome</th>
              <th className="num">Steps</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((x) => (
              <tr key={x.id} className="clickable" onClick={() => navigate(`/sessions/${x.id}`)}>
                <td className="mono small">{x.eval_task_id}</td>
                <td>{x.eval_passed === true ? <Pill tone="ok">pass</Pill> : x.eval_passed === false ? <Pill tone="bad">{x.outcome === "completed" ? "false completion" : "fail"}</Pill> : x.terminal ? <Pill>undecided</Pill> : <Pill tone="live">running</Pill>}</td>
                <td className="small">{x.outcome ?? x.status}</td>
                <td className="num">{x.steps}</td>
                <td className="small dim" style={{ maxWidth: 480, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={x.eval_detail ?? ""}>
                  {x.eval_detail}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {s.per_tag && Object.keys(s.per_tag).length > 0 && (
        <div className="card">
          <div className="card-head">
            <h2>By tag</h2>
          </div>
          <table className="table">
            <tbody>
              {Object.entries(s.per_tag)
                .sort((a, b) => (a[1].pass_rate ?? 0) - (b[1].pass_rate ?? 0))
                .map(([tag, g]) => (
                  <tr key={tag}>
                    <td>{tag}</td>
                    <td className="num">{g.runs}</td>
                    <td className="num">{fmtPct(g.pass_rate)}</td>
                    <td style={{ width: "50%" }}>
                      <div className="bar-track">
                        <div className={`bar-fill ${(g.pass_rate ?? 0) < 0.5 ? "bad" : (g.pass_rate ?? 0) < 0.8 ? "warn" : ""}`} style={{ width: `${(g.pass_rate ?? 0) * 100}%` }} />
                      </div>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
