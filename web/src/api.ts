import type {
  Backend,
  Config,
  ControlCommand,
  ControlExtra,
  CreateSessionBody,
  DemoTask,
  EvalRun,
  Event,
  MetricsSummary,
  ModelsResponse,
  Session,
  Suite,
  TimeseriesPoint,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const qs = (params: Record<string, string | number | boolean | undefined | null>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  config: () => request<Config>("/api/config"),
  backends: () => request<Backend[]>("/api/backends"),
  demoTasks: () => request<DemoTask[]>("/api/demo-tasks"),
  models: () => request<ModelsResponse>("/api/models"),
  refreshModels: () => request<ModelsResponse>("/api/models/refresh", { method: "POST" }),

  sessions: (params: { limit?: number; backend?: string; eval_run_id?: string; status?: string } = {}) =>
    request<Session[]>(`/api/sessions${qs(params)}`),
  session: (id: string) => request<Session>(`/api/sessions/${id}`),
  events: (id: string, after = -1) => request<Event[]>(`/api/sessions/${id}/events${qs({ after })}`),
  createSession: (body: CreateSessionBody) =>
    request<Session>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  control: (id: string, command: ControlCommand, extra: ControlExtra = {}) =>
    request<{ ok: boolean; status: string; control: Record<string, unknown> | null }>(`/api/sessions/${id}/control`, {
      method: "POST",
      body: JSON.stringify({ command, ...extra }),
    }),
  manual: (id: string, action: Record<string, unknown>) =>
    request<{ ok: boolean; error: string | null }>(`/api/sessions/${id}/manual`, {
      method: "POST",
      body: JSON.stringify(action),
    }),

  metrics: (params: { since_s?: number; backend?: string; include_evals?: boolean } = {}) =>
    request<MetricsSummary>(`/api/metrics/summary${qs(params)}`),
  timeseries: (days = 14, backend?: string) => request<TimeseriesPoint[]>(`/api/metrics/timeseries${qs({ days, backend })}`),

  suites: () => request<Suite[]>("/api/evals/suites"),
  evalRuns: () => request<EvalRun[]>("/api/evals/runs"),
  evalRun: (id: string) => request<EvalRun>(`/api/evals/runs/${id}`),
  startEval: (body: { suite: string; model: string; backend?: string; repeats: number; task_ids?: string[]; concurrency?: number }) =>
    request<{ run_id: string; tasks: number }>("/api/evals/run", { method: "POST", body: JSON.stringify(body) }),
};

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}
