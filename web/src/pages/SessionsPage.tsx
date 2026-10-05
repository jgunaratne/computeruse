import { useEffect, useState } from "react";
import { api, wsUrl } from "../api";
import { NewSessionForm } from "../components/NewSessionForm";
import { Empty, Spinner, StatusPill, VerdictPill, useAsync, useNow, useToast } from "../components/ui";
import { navigate } from "../hooks/useRoute";
import { fmtCost, fmtDuration, fmtRelative, truncate } from "../lib/format";
import type { Session } from "../types";

/** Task text for operator-driven browser sessions (sign-in to internal systems, seeding the persistent profile). */
export const MANUAL_TASK =
  "Manual browsing session: the operator is signing in to internal systems and may hand control back with further instructions. " +
  "When resumed, take a screenshot and wait for guidance; do not navigate away or close tabs on your own.";

export function SessionsPage() {
  const toast = useToast();
  const meta = useAsync(async () => {
    const [backends, config, demos] = await Promise.all([api.backends(), api.config(), api.demoTasks()]);
    return { backends, config, demos };
  }, []);
  const [backend, setBackend] = useState<string>("");
  const [hideEvals, setHideEvals] = useState(false);
  const [manualBusy, setManualBusy] = useState(false);
  const list = useAsync(() => api.sessions({ limit: 200, backend: backend || undefined }), [backend], { pollMs: 30000 });
  const now = useNow(true, 5000);

  // Live updates: the global feed pushes a fresh session snapshot with every event.
  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;
    const connect = () => {
      ws = new WebSocket(wsUrl("/api/events/ws"));
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data) as { type: string; session?: Session | null };
        if (msg.type !== "event" || !msg.session) return;
        const s = msg.session;
        list.setData((cur) => {
          if (!cur) return cur;
          if (backend && s.backend !== backend) return cur;
          const idx = cur.findIndex((x) => x.id === s.id);
          if (idx === -1) return [s, ...cur];
          const next = cur.slice();
          next[idx] = s;
          return next;
        });
      };
      ws.onclose = () => {
        if (!closed) timer = window.setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [backend, list.setData]);

  const sessions = (list.data ?? []).filter((s) => !hideEvals || !s.eval_run_id);
  const activeCount = sessions.filter((s) => !s.terminal).length;

  const browser = meta.data?.backends.find((b) => b.id === "browser");
  const canManual = !!browser?.available && !!meta.data?.config.models_available;
  const manualTitle = !browser?.available
    ? `Browser computer unavailable${browser?.reason ? `: ${browser.reason}` : ""}`
    : !meta.data?.config.models_available
      ? "Needs a model provider (the session resumes with the agent once you hand control back)"
      : "Open Chrome on the persistent profile and take control right away — sign in to internal systems once, then every later session is already logged in";
  const startManual = async () => {
    if (!meta.data || manualBusy) return;
    setManualBusy(true);
    try {
      const s = await api.createSession({
        task: MANUAL_TASK,
        backend: "browser",
        model: meta.data.config.model,
        start_paused: true,
        tags: ["manual"],
        backend_options: { profile: "persistent" },
      });
      navigate(`/sessions/${s.id}?control=1`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    } finally {
      setManualBusy(false);
    }
  };

  return (
    <div className="page fade-in">
      <div className="page-head">
        <div>
          <h1>Sessions</h1>
          <p className="sub">Give the agent a task, then watch every screenshot, decision and action as it works.</p>
        </div>
        {meta.data && (
          <button className="btn" id="btn-manual-session" onClick={() => void startManual()} disabled={!canManual || manualBusy} title={manualTitle}>
            {manualBusy ? "Starting…" : "🔐 Sign in / browse manually"}
          </button>
        )}
      </div>

      {meta.data ? <NewSessionForm backends={meta.data.backends} config={meta.data.config} demos={meta.data.demos} /> : meta.error ? <div className="notice bad">Cannot reach the API: {meta.error}</div> : <Spinner />}

      <div className="card" style={{ marginTop: 18 }}>
        <div className="card-head">
          <div className="row">
            <h2>Recent sessions</h2>
            {activeCount > 0 && <span className="pill live">{activeCount} active</span>}
          </div>
          <div className="row">
            <label className="check small">
              <input type="checkbox" checked={hideEvals} onChange={(e) => setHideEvals(e.target.checked)} /> hide eval runs
            </label>
            <select className="select" style={{ width: 170, height: 28 }} value={backend} onChange={(e) => setBackend(e.target.value)} aria-label="Filter by computer">
              <option value="">all computers</option>
              {(meta.data?.backends ?? []).map((b) => (
                <option key={b.id} value={b.id}>
                  {b.label}
                </option>
              ))}
            </select>
          </div>
        </div>
        {list.loading && !list.data ? (
          <div className="empty">
            <Spinner />
          </div>
        ) : sessions.length === 0 ? (
          <Empty>No sessions yet. Start one above — try a scripted demo if you have no API key.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Task</th>
                <th>Computer</th>
                <th>Model</th>
                <th className="num">Steps</th>
                <th className="num">Duration</th>
                <th className="num">Cost</th>
                <th>Verdict</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id} className="clickable" onClick={() => navigate(`/sessions/${s.id}`)} id={`session-${s.id}`}>
                  <td>
                    <StatusPill status={s.status} />
                  </td>
                  <td title={s.task} style={{ maxWidth: 420 }}>
                    <div>{truncate(s.task, 90)}</div>
                    {s.outcome && s.outcome !== "completed" && <div className="small dim">{truncate(s.outcome_reason ?? s.outcome, 90)}</div>}
                    {s.eval_task_id && <div className="small dim mono">eval · {s.eval_task_id}</div>}
                  </td>
                  <td>{s.backend}</td>
                  <td className="mono small">
                    <span className="ellipsis" style={{ maxWidth: 220 }} title={s.model}>
                      {s.model}
                    </span>
                  </td>
                  <td className="num">{s.steps}</td>
                  <td className="num">{fmtDuration(s.duration_ms ?? (s.started_at && !s.terminal ? now - s.started_at * 1000 : null))}</td>
                  <td className="num">{fmtCost(s.cost_usd)}</td>
                  <td>
                    <VerdictPill s={s} />
                  </td>
                  <td className="dim small nowrap">{fmtRelative(s.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
