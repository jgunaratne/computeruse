import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { navigate } from "../hooks/useRoute";
import { fmtRelative } from "../lib/format";
import { MAX_STEPS_CAP } from "../lib/limits";
import type { Backend, Config, CreateSessionBody, DemoTask, ProfileStatus, SecurityKeyForwarding } from "../types";
import { ModelSelect, useModels } from "./ModelSelect";
import { useToast } from "./ui";

type Mode = "model" | "scripted" | "replay";

export function NewSessionForm({ backends, config, demos }: { backends: Backend[]; config: Config; demos: DemoTask[] }) {
  const toast = useToast();
  const live = useModels({ models: config.models, discovery: config.models_discovery, defaultModel: config.model });
  const [mode, setMode] = useState<Mode>(config.models_available ? "model" : "scripted");
  const [task, setTask] = useState("");
  const [backend, setBackend] = useState(backends.find((b) => b.available)?.id ?? "simulated");
  const [model, setModel] = useState(config.model);
  const [demo, setDemo] = useState("");
  const [replayId, setReplayId] = useState("");
  const [maxSteps, setMaxSteps] = useState("");
  const [maxDuration, setMaxDuration] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [allowed, setAllowed] = useState("");
  const [startPaused, setStartPaused] = useState(false);
  const [autoApprove, setAutoApprove] = useState(false);
  const [profile, setProfile] = useState<"persistent" | "ephemeral">(config.browser_profile?.mode ?? "persistent");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedBackend = backends.find((b) => b.id === backend);
  const demoTask = demos.find((d) => d.id === demo);
  const effectiveModel = model.trim();
  const providers = config.model_access.providers;
  // Chrome profile facts come from the backend listing (refreshed per page load), falling back to server config.
  const profileStatus: ProfileStatus | null =
    backend === "browser" ? ((selectedBackend?.options?.profile as ProfileStatus | undefined) ?? config.browser_profile ?? null) : null;
  const securityKey: SecurityKeyForwarding | null =
    backend === "browser" ? ((selectedBackend?.options?.security_key as SecurityKeyForwarding | undefined) ?? null) : null;

  // If discovery later proves the current pick unusable, move to the first verified-available model.
  useEffect(() => {
    const current = live.models.find((m) => m.id === model);
    if (current && !current.available) {
      const next = live.defaultModel ?? live.models.find((m) => m.available)?.id;
      if (next) setModel(next);
    }
  }, [live.models, live.defaultModel, model]);

  useEffect(() => {
    if (mode === "scripted" && !demo && demos.length) setDemo(demos[0].id);
  }, [mode, demo, demos]);
  useEffect(() => {
    if (mode === "scripted" && demoTask) {
      setTask(demoTask.instruction);
      setBackend(demoTask.backend);
    }
  }, [mode, demoTask]);

  const demosBySuite = useMemo(() => {
    const m = new Map<string, DemoTask[]>();
    for (const d of demos) m.set(d.suite, [...(m.get(d.suite) ?? []), d]);
    return m;
  }, [demos]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const body: CreateSessionBody = {
      task: task.trim(),
      backend,
      model: mode === "model" ? effectiveModel : mode === "scripted" ? "scripted" : `replay:${replayId.trim()}`,
      overrides: {},
      start_paused: startPaused,
    };
    if (mode === "scripted") body.demo_task = demo;
    if (maxSteps) body.overrides!.max_steps = Number(maxSteps);
    if (maxDuration) body.overrides!.max_duration_s = Number(maxDuration);
    if (maxCost) body.overrides!.max_cost_usd = Number(maxCost);
    if (autoApprove) body.overrides!.auto_approve = true;
    if (allowed.trim()) body.guardrails = { allowed_domains: allowed.split(/[,\s]+/).filter(Boolean) };
    if (backend === "browser") body.backend_options = { profile };
    setSubmitting(true);
    try {
      const s = await api.createSession(body);
      toast(`Session ${s.id} started on ${s.backend}`);
      navigate(`/sessions/${s.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const unavailableReasons = Object.entries(providers)
    .filter(([, st]) => !st.available)
    .map(([p, st]) => `${p}: ${st.reason}`);

  return (
    <form className="card pad stack" onSubmit={submit} id="new-session-form">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>New task</h2>
        <div className="btn-group" role="tablist">
          <button type="button" className={`btn sm ${mode === "model" ? "toggled" : ""}`} onClick={() => setMode("model")} title={config.models_available ? "Drive the computer with a model" : "No model provider is configured on the server"}>
            Model
          </button>
          <button type="button" className={`btn sm ${mode === "scripted" ? "toggled" : ""}`} onClick={() => setMode("scripted")} title="Replay a reference action script through the full harness (no credentials needed)">
            Scripted demo
          </button>
          <button type="button" className={`btn sm ${mode === "replay" ? "toggled" : ""}`} onClick={() => setMode("replay")} title="Re-issue the model turns recorded in a previous session">
            Replay
          </button>
        </div>
      </div>

      {mode === "model" && !config.models_available && (
        <div className="notice">
          No model provider is available on the server ({unavailableReasons.join("; ")}). Configure one (see README → Model access) and restart, or use a scripted demo.
        </div>
      )}
      {mode === "scripted" && (
        <div className="field">
          <label htmlFor="demo-task">Reference script</label>
          <select id="demo-task" className="select" value={demo} onChange={(e) => setDemo(e.target.value)}>
            {[...demosBySuite.entries()].map(([suite, items]) => (
              <optgroup key={suite} label={suite}>
                {items.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.task_id} — {d.instruction.slice(0, 70)}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <span className="small dim">Runs the exact harness (guardrails, screenshots, verifier) with a canned action sequence instead of a model.</span>
        </div>
      )}
      {mode === "replay" && (
        <div className="field">
          <label htmlFor="replay-id">Source session id</label>
          <input id="replay-id" className="input mono" placeholder="e.g. 3f9a1c2b7d4e" value={replayId} onChange={(e) => setReplayId(e.target.value)} />
        </div>
      )}

      <div className="field">
        <label htmlFor="task">Task</label>
        <textarea id="task" className="textarea" placeholder="e.g. Open Wikipedia, find the article on the Python programming language and tell me when it was first released." value={task} onChange={(e) => setTask(e.target.value)} required />
      </div>

      <div className="form-grid">
        <div className="field">
          <label htmlFor="backend">Computer</label>
          <select id="backend" className="select" value={backend} onChange={(e) => setBackend(e.target.value)}>
            {backends.map((b) => (
              <option key={b.id} value={b.id} disabled={!b.available}>
                {b.label}
                {!b.available ? ` — ${b.reason}` : b.isolated ? "" : " ⚠ real desktop"}
              </option>
            ))}
          </select>
          {selectedBackend && <span className="small dim">{selectedBackend.description}</span>}
        </div>
        {mode === "model" && (
          <ModelSelect
            value={model}
            onChange={setModel}
            models={live.models}
            discovery={live.discovery}
            onRefresh={() => void live.refresh()}
            refreshing={live.refreshing}
            error={live.error}
          />
        )}
        <div className="field">
          <label htmlFor="max-steps">Max steps</label>
          <input
            id="max-steps"
            className="input"
            type="number"
            min={1}
            max={MAX_STEPS_CAP}
            placeholder={String(config.defaults.max_steps)}
            value={maxSteps}
            onChange={(e) => setMaxSteps(e.target.value)}
            title={`Actions the agent may take before the session stops (default ${config.defaults.max_steps}, up to ${MAX_STEPS_CAP}). You can raise it while the session runs.`}
          />
          <span className="small dim">One step = one executed action. Default {config.defaults.max_steps}; raise it later from the session page if the task needs more.</span>
        </div>
      </div>

      {selectedBackend && !selectedBackend.isolated && (
        <div className="notice">
          <b>This computer is your real desktop.</b> The agent will move your mouse and type into whatever is focused. Keep the window visible, keep your hands off the keyboard, and use <b>Pause</b> if anything looks wrong. Dangerous shortcuts are blocked and sensitive text requires approval.
        </div>
      )}

      {backend === "browser" && (
        <div className="field" id="profile-field">
          <label htmlFor="profile-mode">Browser profile</label>
          <select id="profile-mode" className="select" value={profile} onChange={(e) => setProfile(e.target.value as "persistent" | "ephemeral")}>
            <option value="persistent">Persistent — keep cookies, logins and SSO between sessions</option>
            <option value="ephemeral">Ephemeral — fresh profile, discarded when the session ends</option>
          </select>
          {profile === "persistent" && profileStatus ? (
            profileStatus.in_use ? (
              <span className="small" style={{ color: "var(--warn)" }}>
                Another session is using the shared profile right now — this session would get a throwaway <b>copy</b> (existing logins work, new ones are not saved). Wait for it to finish to sign in for good.
              </span>
            ) : (
              <span className="small dim">
                🔐 Cookies and logins are kept in <span className="mono">{profileStatus.path}</span>
                {profileStatus.exists ? ` · ${profileStatus.size_mb} MB · last used ${fmtRelative(profileStatus.last_used)}` : " · not created yet"}. One session at a time can write to it.
              </span>
            )
          ) : (
            <span className="small dim">Nothing from this session survives it; no existing logins are available either.</span>
          )}
          {securityKey && (
            <span className="small" id="security-key-hint" style={{ color: securityKey.available ? "var(--text-3)" : "var(--warn)" }} title={securityKey.detail}>
              {securityKey.available
                ? `🔑 Security-key prompts during sign-in reach your key through ${securityKey.transport === "remote-desktop" ? "your remote-desktop client" : "the SSH agent"}.`
                : "🔑 No security-key forwarding detected — sign in with a one-time security code, or start the server from a terminal inside your remote-desktop session."}
            </span>
          )}
        </div>
      )}

      <details>
        <summary className="small muted" style={{ cursor: "pointer" }}>
          Budgets &amp; guardrails
        </summary>
        <div className="form-grid" style={{ marginTop: 10 }}>
          <div className="field">
            <label htmlFor="max-duration">Max duration (s)</label>
            <input id="max-duration" className="input" type="number" min={10} placeholder={String(config.defaults.max_duration_s)} value={maxDuration} onChange={(e) => setMaxDuration(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="max-cost">Cost cap (USD)</label>
            <input id="max-cost" className="input" type="number" min={0} step="0.1" placeholder={config.defaults.max_cost_usd == null ? "none" : String(config.defaults.max_cost_usd)} value={maxCost} onChange={(e) => setMaxCost(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="allowed">Allowed domains</label>
            <input id="allowed" className="input" placeholder="any (e.g. example.com, wikipedia.org)" value={allowed} onChange={(e) => setAllowed(e.target.value)} />
          </div>
        </div>
        <div className="row wrap" style={{ marginTop: 10, gap: 16 }}>
          <label className="check">
            <input type="checkbox" checked={startPaused} onChange={(e) => setStartPaused(e.target.checked)} /> Start paused (step through manually)
          </label>
          <label className="check">
            <input type="checkbox" checked={autoApprove} onChange={(e) => setAutoApprove(e.target.checked)} /> Auto-approve sensitive actions
          </label>
        </div>
      </details>

      {error && <div className="form-error">{error}</div>}
      <div className="row">
        <button className="btn primary" type="submit" disabled={submitting || !task.trim() || (mode === "replay" && !replayId.trim()) || (mode === "model" && !effectiveModel)} id="btn-start">
          {submitting ? "Starting…" : "Start session"}
        </button>
        <span className="small dim">Defaults: {config.defaults.max_steps} steps · {Math.round(config.defaults.max_duration_s / 60)} min{config.defaults.max_cost_usd != null ? ` · $${config.defaults.max_cost_usd} cap` : ""}</span>
      </div>
    </form>
  );
}
