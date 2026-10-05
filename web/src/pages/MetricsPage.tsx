import { useState } from "react";
import { api } from "../api";
import { Spinner, useAsync } from "../components/ui";
import { fmtCost, fmtMs, fmtPct, outcomeLabel } from "../lib/format";
import type { Bucket, Outcome } from "../types";

const WINDOWS: { label: string; s?: number }[] = [
  { label: "24h", s: 86400 },
  { label: "7d", s: 7 * 86400 },
  { label: "30d", s: 30 * 86400 },
  { label: "all" },
];

export function MetricsPage() {
  const [win, setWin] = useState(1);
  const [includeEvals, setIncludeEvals] = useState(true);
  const [backend, setBackend] = useState("");
  const backends = useAsync(() => api.backends(), []);
  const m = useAsync(() => api.metrics({ since_s: WINDOWS[win].s, backend: backend || undefined, include_evals: includeEvals }), [win, backend, includeEvals], { pollMs: 20000 });
  const ts = useAsync(() => api.timeseries(14, backend || undefined), [backend], { pollMs: 60000 });
  const d = m.data;

  return (
    <div className="page fade-in">
      <div className="page-head">
        <div>
          <h1>Metrics</h1>
          <p className="sub">Task success, where sessions fail, how often the harness has to intervene, and what it costs.</p>
        </div>
        <div className="row">
          <label className="check small">
            <input type="checkbox" checked={includeEvals} onChange={(e) => setIncludeEvals(e.target.checked)} /> include evals
          </label>
          <select className="select" style={{ width: 160, height: 28 }} value={backend} onChange={(e) => setBackend(e.target.value)} aria-label="Computer">
            <option value="">all computers</option>
            {(backends.data ?? []).map((b) => (
              <option key={b.id} value={b.id}>
                {b.label}
              </option>
            ))}
          </select>
          <div className="btn-group">
            {WINDOWS.map((w, i) => (
              <button key={w.label} className={`btn sm ${i === win ? "toggled" : ""}`} onClick={() => setWin(i)}>
                {w.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {!d ? (
        m.error ? <div className="notice bad">{m.error}</div> : <Spinner />
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          <div className="stats">
            <Stat label="Task success rate" value={fmtPct(d.success_rate, 1)} hint={`${d.sessions_ended} finished · ${d.evaluated_sessions} verified by checker`} />
            <Stat label="False completions" value={String(d.false_completions)} hint="model said done, verifier disagreed" tone={d.false_completions ? "bad" : undefined} />
            <Stat label="Median steps (success)" value={d.median_steps_success == null ? "–" : String(d.median_steps_success)} hint={`all sessions: ${d.median_steps_all ?? "–"}`} />
            <Stat label="Model latency p50 / p95" value={`${fmtMs(d.p50_model_latency_ms)} / ${fmtMs(d.p95_model_latency_ms)}`} hint={`${d.model_calls} calls · ${d.model_retries} retries`} />
            <Stat label="Action latency p50 / p95" value={`${fmtMs(d.p50_action_latency_ms)} / ${fmtMs(d.p95_action_latency_ms)}`} hint={`${d.actions} actions · ${fmtPct(d.action_error_rate, 1)} errored`} />
            <Stat label="Harness interventions" value={String(Object.values(d.guardrail_interventions).reduce((a, b) => a + b, 0) + d.stuck_nudges)} hint={`${d.stuck_nudges} stuck nudges · ${d.approvals.requested} approvals (${d.approvals.rejected} rejected)`} />
            <Stat label="Cost" value={fmtCost(d.total_cost_usd)} hint={`mean ${fmtCost(d.mean_cost_usd)} per session`} />
            <Stat label="Active now" value={String(d.sessions_active)} hint={`${d.sessions_total} sessions in window`} />
          </div>

          <div className="card">
            <div className="card-head">
              <h2>Daily volume &amp; success (14 days)</h2>
              <span className="small dim">bar = sessions, fill = success share</span>
            </div>
            <div className="card-body">
              {ts.data ? <Trend points={ts.data} /> : <Spinner />}
            </div>
          </div>

          <div className="two-col">
            <div className="card">
              <div className="card-head">
                <h2>Why sessions fail</h2>
                <span className="small dim">{Object.values(d.failure_taxonomy).reduce((a, b) => a + b, 0)} failures</span>
              </div>
              <div className="card-body">
                <Bars rows={Object.entries(d.failure_taxonomy).map(([k, v]) => ({ name: outcomeLabel[k as Outcome] ?? k, value: v }))} tone="bad" empty="No failures in this window." />
              </div>
            </div>
            <div className="card">
              <div className="card-head">
                <h2>Guardrail interventions</h2>
              </div>
              <div className="card-body">
                <Bars rows={Object.entries(d.guardrail_interventions).map(([k, v]) => ({ name: k, value: v }))} tone="warn" empty="No guardrail blocks or approvals." />
              </div>
            </div>
            <div className="card">
              <div className="card-head">
                <h2>Action mix</h2>
              </div>
              <div className="card-body">
                <Bars rows={Object.entries(d.action_mix).map(([k, v]) => ({ name: k, value: v }))} tone="info" empty="No actions yet." />
              </div>
            </div>
            <div className="card">
              <div className="card-head">
                <h2>Success by computer &amp; model</h2>
              </div>
              <div className="card-body">
                <BucketTable buckets={{ ...prefix("computer", d.by_backend), ...prefix("model", d.by_model) }} />
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h2>Success by tag</h2>
              <span className="small dim">lowest first — where to focus</span>
            </div>
            <div className="card-body">
              <BucketTable buckets={d.by_tag} sort />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const prefix = (p: string, b: Record<string, Bucket>) => Object.fromEntries(Object.entries(b).map(([k, v]) => [`${p} · ${k}`, v]));

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "bad" }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value" style={tone === "bad" && value !== "0" ? { color: "var(--bad)" } : undefined}>
        {value}
      </div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

function Bars({ rows, tone, empty }: { rows: { name: string; value: number }[]; tone?: "bad" | "warn" | "info"; empty: string }) {
  if (!rows.length) return <div className="dim small">{empty}</div>;
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className="bars">
      {rows
        .sort((a, b) => b.value - a.value)
        .map((r) => (
          <div className="bar-row" key={r.name}>
            <span className="name" title={r.name}>
              {r.name}
            </span>
            <div className="bar-track">
              <div className={`bar-fill ${tone ?? ""}`} style={{ width: `${(r.value / max) * 100}%` }} />
            </div>
            <span className="num">{r.value}</span>
          </div>
        ))}
    </div>
  );
}

function BucketTable({ buckets, sort }: { buckets: Record<string, Bucket>; sort?: boolean }) {
  const rows = Object.entries(buckets);
  if (!rows.length) return <div className="dim small">Nothing to show yet.</div>;
  if (sort) rows.sort((a, b) => (a[1].success_rate ?? 0) - (b[1].success_rate ?? 0));
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Segment</th>
          <th className="num">Sessions</th>
          <th className="num">Success</th>
          <th style={{ width: "40%" }}></th>
          <th className="num">Median steps</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([k, b]) => (
          <tr key={k}>
            <td>{k}</td>
            <td className="num">{b.sessions}</td>
            <td className="num">{fmtPct(b.success_rate)}</td>
            <td>
              <div className="bar-track">
                <div className={`bar-fill ${(b.success_rate ?? 0) < 0.5 ? "bad" : (b.success_rate ?? 0) < 0.8 ? "warn" : ""}`} style={{ width: `${(b.success_rate ?? 0) * 100}%` }} />
              </div>
            </td>
            <td className="num">{b.median_steps ?? "–"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Trend({ points }: { points: { day: string; sessions: number; successes: number; success_rate: number | null }[] }) {
  const max = Math.max(...points.map((p) => p.sessions), 1);
  return (
    <div className="trend">
      {points.map((p) => (
        <div className="col" key={p.day}>
          <div className="tip">
            {p.day}: {p.sessions} sessions · {fmtPct(p.success_rate)}
          </div>
          <div className="v" style={{ height: `${Math.max(2, (p.sessions / max) * 100)}%` }}>
            <i style={{ height: `${(p.success_rate ?? 0) * 100}%` }} />
          </div>
          <div className="d">{p.day.slice(5)}</div>
        </div>
      ))}
    </div>
  );
}
