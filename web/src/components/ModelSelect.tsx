import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { fmtRelative } from "../lib/format";
import type { DiscoveryState, ModelInfo } from "../types";

export const PROVIDER_LABEL: Record<ModelInfo["provider"], string> = {
  vertex: "Claude on Vertex AI",
  anthropic: "Anthropic API",
  gemini: "Gemini",
  antigravity: "Antigravity (local Language Server)",
};
export const TOOL_LABEL: Record<ModelInfo["tool"], string> = {
  builtin: "native computer tool",
  toolset: "computer toolset",
  schema: "function-call tool",
  json: "JSON action protocol",
};

const CUSTOM = "__custom__";

/**
 * Keeps the model list live: starts from what the page already has, polls `/api/models`
 * while discovery is running, and exposes a forced refresh.
 */
export function useModels(initial: { models: ModelInfo[]; discovery: DiscoveryState; defaultModel: string | null }) {
  const [models, setModels] = useState(initial.models);
  const [discovery, setDiscovery] = useState(initial.discovery);
  const [defaultModel, setDefaultModel] = useState(initial.defaultModel);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const apply = useCallback((r: { models: ModelInfo[]; discovery: DiscoveryState; default: string | null }) => {
    if (!alive.current) return;
    setModels(r.models);
    setDiscovery(r.discovery);
    setDefaultModel(r.default);
  }, []);

  // Poll while a discovery run is in flight (startup or someone else's refresh).
  useEffect(() => {
    if (discovery.status !== "running") return;
    const id = window.setInterval(() => void api.models().then(apply).catch(() => undefined), 1500);
    return () => window.clearInterval(id);
  }, [discovery.status, apply]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      apply(await api.refreshModels());
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (alive.current) setRefreshing(false);
    }
  }, [apply]);

  return { models, discovery, defaultModel, refresh, refreshing: refreshing || discovery.status === "running", error };
}

export function discoverySummary(d: DiscoveryState): string {
  if (d.status === "off") return "discovery off — showing suggested ids";
  if (d.status === "running") return "verifying model access…";
  if (d.status === "error") return `discovery failed: ${Object.values(d.errors).join("; ")}`;
  if (d.status === "idle") return "not verified yet";
  const what = d.probed ? `${d.verified} verified by a live request` : `${d.count} listed (not verified)`;
  const where = d.projects.length ? ` · projects ${d.projects.join(", ")}` : "";
  const errs = Object.keys(d.errors).length ? ` · ${Object.entries(d.errors).map(([p, e]) => `${p}: ${e}`).join("; ")}` : "";
  return `${what}${where} · ${fmtRelative(d.refreshed_at)}${errs}`;
}

function mark(m: ModelInfo): string {
  if (!m.available) return "✗";
  return m.verified ? "✓" : "?";
}

export function ModelSelect({
  value,
  onChange,
  models,
  discovery,
  onRefresh,
  refreshing,
  error,
  allowCustom = true,
  extraOptions,
  compact = false,
  id = "model",
}: {
  value: string;
  onChange: (id: string) => void;
  models: ModelInfo[];
  discovery: DiscoveryState;
  onRefresh?: () => void;
  refreshing?: boolean;
  error?: string | null;
  allowCustom?: boolean;
  /** Rendered before the provider groups (e.g. "scripted"). */
  extraOptions?: { value: string; label: string }[];
  compact?: boolean;
  id?: string;
}) {
  const [custom, setCustom] = useState("");
  const byProvider = useMemo(() => {
    const m = new Map<ModelInfo["provider"], ModelInfo[]>();
    for (const info of models) m.set(info.provider, [...(m.get(info.provider) ?? []), info]);
    return m;
  }, [models]);
  const known = models.some((m) => m.id === value) || (extraOptions ?? []).some((o) => o.value === value);
  const selectValue = known ? value : allowCustom && value ? CUSTOM : value;
  const selected = models.find((m) => m.id === value);

  const pick = (v: string) => {
    if (v === CUSTOM) {
      onChange(custom.trim());
      return;
    }
    onChange(v);
  };

  const select = (
    <select
      id={id}
      className="select"
      style={compact ? { height: 28, width: 230 } : undefined}
      value={selectValue}
      onChange={(e) => pick(e.target.value)}
      aria-label="Model"
      title={selected ? `${selected.id} — ${selected.reason || "available"}` : undefined}
    >
      {(extraOptions ?? []).map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
      {[...byProvider.entries()].map(([provider, items]) => (
        <optgroup key={provider} label={PROVIDER_LABEL[provider] ?? provider}>
          {items.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.available} title={m.reason}>
              {mark(m)} {m.label}
              {compact ? "" : ` (${m.id})`}
              {m.provider === "vertex" && m.project && discovery.projects.length > 1 ? ` · ${m.project}` : ""}
              {!m.available ? ` — ${m.reason}` : ""}
            </option>
          ))}
        </optgroup>
      ))}
      {allowCustom && <option value={CUSTOM}>Other model id…</option>}
    </select>
  );

  const refreshButton = onRefresh && (
    <button
      type="button"
      className="btn sm ghost"
      onClick={onRefresh}
      disabled={refreshing || discovery.status === "off"}
      title={discovery.status === "off" ? "Discovery is disabled (COMPUTERUSE_MODEL_DISCOVERY=off)" : "Re-list the provider catalogues and re-verify access with a 1-token request per model"}
      aria-label="Refresh models"
    >
      {refreshing ? <span className="spinner" aria-hidden /> : "↻"}
      {compact ? "" : refreshing ? " Verifying…" : " Refresh"}
    </button>
  );

  if (compact) {
    return (
      <span className="row" style={{ gap: 6 }}>
        {select}
        {refreshButton}
      </span>
    );
  }

  return (
    <div className="field">
      <label htmlFor={id}>Model</label>
      <div className="row" style={{ gap: 8 }}>
        {select}
        {refreshButton}
      </div>
      {selectValue === CUSTOM && (
        <input
          className="input mono"
          placeholder="e.g. vertex:claude-sonnet-4-5@20250929, gemini-3.1-pro-preview or antigravity:<model-id>"
          value={custom || (known ? "" : value)}
          onChange={(e) => {
            setCustom(e.target.value);
            onChange(e.target.value.trim());
          }}
          aria-label="Custom model id"
        />
      )}
      {selectValue !== CUSTOM && selected && (
        <span className="small dim">
          via {PROVIDER_LABEL[selected.provider]} · {TOOL_LABEL[selected.tool]}
          {selected.project ? ` · project ${selected.project}` : ""}
          {selected.verified ? " · ✓ verified" : selected.available ? " · ? not verified" : ""}
          {selected.stage && selected.stage !== "GA" ? ` · ${selected.stage}` : ""}
        </span>
      )}
      <span className={`small ${discovery.status === "error" || error ? "bad" : "muted"}`}>
        {error ?? discoverySummary(discovery)}
      </span>
    </div>
  );
}
