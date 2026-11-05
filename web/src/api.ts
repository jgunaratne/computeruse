import type {
  Backend,
  Chat,
  ChatIndex,
  ChatSendBody,
  ChatSendResponse,
  Config,
  ControlCommand,
  ControlExtra,
  CreateSessionBody,
  DemoTask,
  EvalRun,
  Event,
  Flow,
  FlowCatalog,
  FlowRun,
  FlowRunSummary,
  FlowSummary,
  MetricsSummary,
  ModelsResponse,
  PermissionLevel,
  PermissionsResponse,
  Schedule,
  ScheduleCatalog,
  ScheduleRun,
  ScheduleTargetKind,
  ScheduleTrigger,
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

  chatIndex: () => request<ChatIndex>("/api/chat"),
  createChat: (body: { model?: string; title?: string } = {}) =>
    request<Chat>("/api/chat", { method: "POST", body: JSON.stringify(body) }),
  /** `waitS > 0` long-polls: the server answers early once a pending reply has landed. */
  chat: (id: string, waitS = 0, signal?: AbortSignal) => request<Chat>(`/api/chat/${id}${qs({ wait_s: waitS })}`, { signal }),
  sendChat: (id: string, body: ChatSendBody) =>
    request<ChatSendResponse>(`/api/chat/${id}/messages`, { method: "POST", body: JSON.stringify(body) }),
  cancelChat: (id: string) => request<Chat>(`/api/chat/${id}/cancel`, { method: "POST" }),
  deleteChat: (id: string) => request<void>(`/api/chat/${id}`, { method: "DELETE" }),

  flowCatalog: () => request<FlowCatalog>("/api/flows/catalog"),
  flows: () => request<FlowSummary[]>("/api/flows"),
  createFlow: (body: { name?: string; template?: string } = {}) =>
    request<Flow>("/api/flows", { method: "POST", body: JSON.stringify(body) }),
  flow: (id: string) => request<Flow>(`/api/flows/${id}`),
  saveFlow: (flow: Flow) => request<Flow>(`/api/flows/${flow.id}`, { method: "PUT", body: JSON.stringify(flow) }),
  deleteFlow: (id: string) => request<void>(`/api/flows/${id}`, { method: "DELETE" }),
  startFlowRun: (id: string, body: { goal?: string } = {}) =>
    request<FlowRun>(`/api/flows/${id}/runs`, { method: "POST", body: JSON.stringify(body) }),
  flowRuns: (id: string) => request<FlowRunSummary[]>(`/api/flows/${id}/runs`),
  /** `waitS > 0` long-polls until the run's version is newer than `after` (or it ends). */
  flowRun: (runId: string, after = -1, waitS = 0, signal?: AbortSignal) =>
    request<FlowRun>(`/api/flow-runs/${runId}${qs({ after, wait_s: waitS })}`, { signal }),
  cancelFlowRun: (runId: string) => request<FlowRun>(`/api/flow-runs/${runId}/cancel`, { method: "POST" }),

  scheduleCatalog: () => request<ScheduleCatalog>("/api/schedules/catalog"),
  schedules: (status?: string) => request<Schedule[]>(`/api/schedules${qs({ status })}`),
  schedule: (id: string) => request<Schedule>(`/api/schedules/${id}`),
  createSchedule: (body: {
    title: string;
    plan: string;
    target_kind?: ScheduleTargetKind;
    backend?: string;
    model?: string | null;
    flow_id?: string | null;
    demo_task?: string | null;
    trigger?: Partial<ScheduleTrigger>;
    status?: string;
  }) => request<Schedule>("/api/schedules", { method: "POST", body: JSON.stringify(body) }),
  updateSchedule: (id: string, body: Record<string, unknown>) =>
    request<Schedule>(`/api/schedules/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  setScheduleStatus: (id: string, status: string, pauseReason?: string) =>
    request<Schedule>(`/api/schedules/${id}/status`, {
      method: "POST",
      body: JSON.stringify({ status, pause_reason: pauseReason }),
    }),
  markScheduleRead: (id: string) => request<Schedule>(`/api/schedules/${id}/read`, { method: "POST" }),
  runScheduleNow: (id: string, wait = true) =>
    request<{ run: ScheduleRun; schedule: Schedule }>(`/api/schedules/${id}/run${qs({ wait })}`, { method: "POST" }),
  deleteSchedule: (id: string) => request<void>(`/api/schedules/${id}`, { method: "DELETE" }),

  permissions: () => request<PermissionsResponse>("/api/permissions"),
  updatePermissions: (body: {
    updates?: Record<string, PermissionLevel>;
    category_id?: string;
    level?: PermissionLevel;
    reset?: boolean;
  }) => request<PermissionsResponse>("/api/permissions", { method: "PUT", body: JSON.stringify(body) }),
};

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}
