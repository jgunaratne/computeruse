// Wire types mirrored from the Python backend (computeruse/telemetry/events.py,
// computeruse/server/schemas.py). Keep in sync by hand; the surface is small.

export type SessionStatus =
  | "created"
  | "starting"
  | "running"
  | "paused"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type Outcome =
  | "completed"
  | "max_steps"
  | "timeout"
  | "budget_exceeded"
  | "stuck"
  | "model_error"
  | "computer_error"
  | "guardrail_blocked"
  | "cancelled"
  | "internal_error";

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
}

export interface OperatorControlState {
  active: boolean;
  since: number;
}

export interface ProfileLease {
  path: string;
  mode: "persistent" | "ephemeral";
  /** True when Chrome writes straight into the shared profile (logins are kept). */
  persistent: boolean;
  source: string | null;
  note: string;
}

export interface ProfileStatus {
  mode: "persistent" | "ephemeral";
  path: string;
  exists: boolean;
  in_use: boolean;
  size_mb: number;
  last_used: number | null;
}

/** Whether a security-key touch during sign-in can reach a real key (see computeruse/computer/sso.py). */
export interface SecurityKeyForwarding {
  transport: "remote-desktop" | "ssh-agent" | null;
  available: boolean;
  detail: string;
  crd_session: boolean;
  ssh_auth_sock: string | null;
}

/** Limits the loop enforces; adjustable while the session runs (`budget` control command). */
export interface Budget {
  max_steps: number;
  max_turns: number | null;
  max_duration_s: number;
  max_cost_usd: number | null;
}

export interface SessionMetadata {
  operator_control?: OperatorControlState;
  budget?: Budget;
  /** What the model client reported about itself when the session ended (e.g. the Antigravity conversation id). */
  model_client?: Record<string, unknown>;
  computer?: { profile?: ProfileLease | null; devtools_url?: string | null };
  final_text?: string;
  guardrails?: Record<string, unknown>;
  demo_task?: string;
  [key: string]: unknown;
}

export interface Session {
  id: string;
  task: string;
  backend: string;
  model: string;
  status: SessionStatus;
  outcome: Outcome | null;
  outcome_reason: string | null;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  steps: number;
  turns: number;
  duration_ms: number | null;
  usage: Usage;
  cost_usd: number | null;
  display_width: number | null;
  display_height: number | null;
  live_view_url: string | null;
  tags: string[];
  eval_run_id: string | null;
  eval_task_id: string | null;
  eval_passed: boolean | null;
  eval_detail: string | null;
  metadata: SessionMetadata;
  success: boolean | null;
  terminal: boolean;
}

export type EventType =
  | "session.started"
  | "session.paused"
  | "session.resumed"
  | "session.ended"
  | "turn.started"
  | "model.called"
  | "model.retry"
  | "assistant.text"
  | "action.proposed"
  | "guardrail.decision"
  | "approval.requested"
  | "approval.resolved"
  | "action.executed"
  | "frame"
  | "preview"
  | "stuck.nudged"
  | "user.instruction"
  | "manual.action"
  | "operator.control"
  | "budget.changed"
  | "error";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type EventData = Record<string, any>;

export interface Event {
  session_id: string;
  seq: number;
  ts: number;
  type: EventType;
  data: EventData;
}

export interface FrameRef {
  seq: number;
  url: string;
  width: number;
  height: number;
  sha1?: string;
  kind?: string;
  step?: number | null;
}

export interface Backend {
  id: string;
  label: string;
  description: string;
  available: boolean;
  reason: string | null;
  isolated: boolean;
  options: Record<string, unknown>;
}

export interface ModelInfo {
  id: string;
  label: string;
  provider: "anthropic" | "vertex" | "gemini" | "antigravity";
  available: boolean;
  reason: string;
  /** How the computer tool reaches the model; `json` = Antigravity's reply protocol (no function calling). */
  tool: "builtin" | "toolset" | "schema" | "json";
  /** Availability confirmed by a live 1-token request (discovery), not just inferred from credentials. */
  verified: boolean;
  source: "suggested" | "configured" | "vertex-catalog" | "gemini-api" | "anthropic-api" | "antigravity" | string;
  /** Vertex routes: the GCP project that serves the model. */
  project: string | null;
  version: string;
  stage: string;
}

export interface DiscoveryState {
  mode: "off" | "list" | "probe";
  status: "off" | "idle" | "running" | "done" | "error";
  probed: boolean;
  refreshed_at: number | null;
  duration_ms: number | null;
  projects: string[];
  errors: Record<string, string>;
  count: number;
  verified: number;
  max_age_s: number;
}

export interface ModelsResponse {
  models: ModelInfo[];
  default: string | null;
  discovery: DiscoveryState;
}

export interface ProviderStatus {
  available: boolean;
  reason: string;
  detail: Record<string, unknown>;
}

export interface Config {
  model: string;
  models: ModelInfo[];
  models_available: boolean;
  model_access: {
    providers: Record<string, ProviderStatus>;
    project: string | null;
    projects: string[];
    preference: string;
    discovery: DiscoveryState;
  };
  models_discovery: DiscoveryState;
  tool_version: string;
  preview_fps: number;
  control_preview_fps: number;
  browser_size: string;
  browser_profile: ProfileStatus;
  defaults: {
    max_steps: number;
    max_duration_s: number;
    max_cost_usd: number | null;
    stuck_threshold: number;
    screenshot_history: number;
    settle_ms: number;
    approval_timeout_s: number;
  };
  guardrails: {
    allowed_domains: string[];
    blocked_domains: string[];
    blocked_keys: string[];
    desktop_blocked_keys: string[];
    max_type_length: number;
  };
}

export interface DemoTask {
  id: string;
  suite: string;
  task_id: string;
  backend: string;
  instruction: string;
  tags: string[];
  checker: Record<string, unknown>;
}

export interface SuiteTask {
  id: string;
  instruction: string;
  tags: string[];
  max_steps: number | null;
  checker: Record<string, unknown>;
  has_script: boolean;
  guardrails: Record<string, unknown>;
}

export interface Suite {
  name: string;
  backend: string;
  description: string;
  path: string | null;
  tasks: SuiteTask[];
}

export interface EvalTaskSummary {
  runs: number;
  passed: number;
  pass_rate: number | null;
  mean_steps: number | null;
  details: { session_id: string; passed: boolean | null; outcome: string | null; detail: string | null; steps: number; cost_usd: number | null }[];
}

export interface EvalSummary {
  run_id: string;
  suite: string;
  backend: string;
  model: string;
  runs: number;
  passed: number;
  pass_rate: number | null;
  false_completions: number;
  mean_steps: number | null;
  outcomes: Record<string, number>;
  per_task: Record<string, EvalTaskSummary>;
  per_tag: Record<string, { runs: number; passed: number; pass_rate: number | null }>;
  total_cost_usd: number;
  wall_s: number;
  error?: string;
}

export interface EvalRun {
  id: string;
  created_at: number;
  suite: string;
  model: string;
  backend: string | null;
  status: "running" | "finished" | "failed";
  summary: Partial<EvalSummary>;
  sessions?: Session[];
}

export interface Bucket {
  sessions: number;
  successes: number;
  success_rate: number | null;
  median_steps: number | null;
}

export interface MetricsSummary {
  window_s: number | null;
  sessions_total: number;
  sessions_ended: number;
  sessions_active: number;
  success_rate: number | null;
  evaluated_sessions: number;
  false_completions: number;
  median_steps_success: number | null;
  median_steps_all: number | null;
  median_duration_s: number | null;
  p50_model_latency_ms: number | null;
  p95_model_latency_ms: number | null;
  p50_action_latency_ms: number | null;
  p95_action_latency_ms: number | null;
  model_calls: number;
  model_retries: number;
  actions: number;
  action_error_rate: number | null;
  action_mix: Record<string, number>;
  failure_taxonomy: Record<string, number>;
  guardrail_interventions: Record<string, number>;
  approvals: { requested: number; approved: number; rejected: number };
  stuck_nudges: number;
  total_cost_usd: number;
  mean_cost_usd: number | null;
  by_backend: Record<string, Bucket>;
  by_model: Record<string, Bucket>;
  by_tag: Record<string, Bucket>;
}

export interface TimeseriesPoint {
  day: string;
  sessions: number;
  successes: number;
  cost_usd: number;
  success_rate: number | null;
}

export type ControlCommand = "pause" | "resume" | "step" | "cancel" | "approve" | "reject" | "instruct" | "take_control" | "release_control" | "budget";

export interface ControlExtra {
  approval_id?: string;
  text?: string;
  resume?: boolean;
  /** `budget`: any subset of the limits to change on the running session. */
  max_steps?: number;
  max_turns?: number;
  max_duration_s?: number;
  max_cost_usd?: number;
}

export interface CreateSessionBody {
  task?: string;
  backend?: string;
  model?: string;
  demo_task?: string;
  overrides?: Partial<{
    max_steps: number;
    max_duration_s: number;
    max_cost_usd: number;
    auto_approve: boolean;
    system_prompt_extra: string;
  }>;
  guardrails?: { allowed_domains?: string[]; blocked_domains?: string[] };
  backend_options?: { profile?: "persistent" | "ephemeral"; start_url?: string; [key: string]: unknown };
  tags?: string[];
  start_paused?: boolean;
}

/** One raw operator input, in the computer's action vocabulary (see computeruse/computer/actions.py). */
export type InputAction = Record<string, unknown> & { action: string };
