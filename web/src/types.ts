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

// --- chat (the console's side panel; see computeruse/server/chat.py) ---------------------

export interface ChatModel {
  id: string;
  label: string;
  supports_images: boolean;
  available: boolean;
  reason: string;
}

/** What the console attached to a user message (the session timeline the chat had not seen yet). */
export interface ChatContext {
  session_id: string;
  events: number;
  first: boolean;
  screenshot: boolean;
  after_seq: number;
  until_seq: number;
  note?: string;
}

export interface ChatActionExecution {
  type: "command" | "action";
  name: string;
  params: Record<string, unknown>;
  ok: boolean;
  summary: string;
  error?: string | null;
  session_id?: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  created_at: number;
  model: string | null;
  model_label: string | null;
  thinking: string;
  usage: Usage | null;
  latency_ms: number | null;
  stop_reason: string;
  error: string | null;
  pending: boolean;
  context: ChatContext | null;
  actions?: ChatActionExecution[];
}

export interface ChatSummary {
  id: string;
  title: string | null;
  model: string;
  model_label: string;
  created_at: number;
  updated_at: number;
  pending: boolean;
  conversation_id: string | null;
  message_count: number;
  sessions: string[];
}

export interface Chat extends ChatSummary {
  messages: ChatMessage[];
}

export interface ChatIndex {
  available: boolean;
  reason: string;
  default_model: string | null;
  models: ChatModel[];
  chats: ChatSummary[];
}

export interface ChatSendBody {
  text: string;
  model?: string;
  session_id?: string;
  screenshot?: boolean;
}

export interface ChatSendResponse {
  user_message: ChatMessage;
  message: ChatMessage;
  chat: ChatSummary;
}

// --- agent flows (the Agents tab; see computeruse/flows) ---------------------------------

export type FlowPortType = "text" | "screen";

export interface FlowPortSpec {
  name: string;
  type: FlowPortType;
  label: string;
  /** Inputs only: true = a message here starts the node's work; false = latched as context. */
  trigger: boolean;
  doc: string;
}

export type FlowFieldKind = "text" | "textarea" | "number" | "select" | "role" | "model" | "computer_model" | "backend" | "demo_task";

export interface FlowFieldSpec {
  key: string;
  label: string;
  kind: FlowFieldKind;
  default: string | number;
  options?: { value: string; label: string }[];
  min?: number;
  max?: number;
  placeholder: string;
  doc: string;
}

export type FlowCategory = "start" | "agent" | "flow" | "memory" | "end" | "note";

export interface FlowNodeTypeSpec {
  type: string;
  title: string;
  desc: string;
  category: FlowCategory;
  inputs: FlowPortSpec[];
  outputs: FlowPortSpec[];
  fields: FlowFieldSpec[];
  agent: boolean;
  needs_antigravity: boolean;
}

export interface FlowTemplateSummary {
  id: string;
  name: string;
  description: string;
  tags: string[];
  needs_antigravity: boolean;
}

export interface FlowCatalog {
  node_types: FlowNodeTypeSpec[];
  port_types: Record<FlowPortType, { label: string; doc: string }>;
  roles: { value: string; label: string; instructions: string }[];
  templates: FlowTemplateSummary[];
  antigravity: { available: boolean; reason: string; default_model: string | null; models: ChatModel[] };
  limits: { max_nodes: number; max_edges: number };
}

export interface FlowNode {
  id: string;
  type: string;
  label: string;
  pos: [number, number];
  config: Record<string, string | number>;
  muted: boolean;
}

export interface FlowEdge {
  id: string;
  source: string;
  source_port: string;
  target: string;
  target_port: string;
}

export interface FlowViewport {
  x: number;
  y: number;
  zoom: number;
}

export interface Flow {
  id: string;
  name: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  viewport: FlowViewport;
  settings: { max_agent_calls: number };
  template: string | null;
  created_at: number;
  updated_at: number;
}

export type FlowRunStatus = "running" | "completed" | "incomplete" | "failed" | "cancelled" | "interrupted";

export interface FlowRunSummary {
  id: string;
  flow_id: string;
  flow_name: string;
  status: FlowRunStatus;
  reason: string;
  started_at: number;
  ended_at: number | null;
  invocations: number;
  max_invocations: number;
}

export interface FlowSummary {
  id: string;
  name: string;
  template: string | null;
  created_at: number;
  updated_at: number;
  nodes: number;
  edges: number;
  last_run: { id: string; status: FlowRunStatus; started_at: number; ended_at: number | null } | null;
}

export interface FlowFrame {
  session_id: string;
  seq: number;
  url: string;
}

export interface FlowSignal {
  kind: string;
  text: string;
  chars: number;
  frame: FlowFrame | null;
  source: string;
  source_port: string;
  source_label: string;
  meta: Record<string, unknown>;
  ts: number;
}

export type FlowNodeStatus = "idle" | "waiting" | "running" | "done" | "stopped" | "error" | "muted";

export interface FlowNodeState {
  id: string;
  type: string;
  label: string;
  status: FlowNodeStatus;
  detail: string;
  runs: number;
  queued: number;
  last_input: FlowSignal | null;
  last_output: FlowSignal | null;
  error: string | null;
  session_id: string | null;
  sessions: string[];
  steps: number;
  frame: FlowFrame | null;
  verdict: "pass" | "revise" | "max_rounds" | null;
  rounds: number;
  received: number;
  expected: number;
  entries: number;
  result: string | null;
  conversation_id: string | null;
  updated_at: number;
}

export interface FlowLogEntry {
  seq: number;
  ts: number;
  type: "message" | "node" | "run" | "drop";
  node?: string;
  source?: string;
  source_port?: string;
  target?: string;
  target_port?: string;
  kind?: string;
  text?: string;
  status?: string;
  frame?: boolean;
  dropped?: boolean;
  session_id?: string;
}

export interface FlowRun extends FlowRunSummary {
  version: number;
  goal: string | null;
  nodes: Record<string, FlowNodeState>;
  log: FlowLogEntry[];
  results: { node: string; label: string; text: string; verdict: string | null; frame: FlowFrame | null }[];
}

// --- scheduled automations & granular permissions (the Schedules tab) --------------------

export type ScheduleFrequency = "hourly" | "daily" | "weekly" | "monthly" | "once";
export type ScheduleStatus = "active" | "paused" | "completed";
export type PauseReason = "user_action" | "execution_failed" | null;
export type ScheduleTargetKind = "antigravity_chat" | "session" | "flow";
export type PermissionLevel = "allow" | "ask" | "deny";

export interface ScheduleTrigger {
  frequency: ScheduleFrequency;
  minute_of_hour: number;
  time_of_day: string;
  days_of_week: number[];
  day_of_month: number;
  run_date: string | null;
  condition: string;
  end_date: string | null;
}

export interface ScheduleRun {
  id: string;
  schedule_id: string;
  schedule_title: string;
  target_kind: ScheduleTargetKind;
  status: "running" | "completed" | "failed";
  started_at: number;
  ended_at: number | null;
  duration_ms: number | null;
  summary: string;
  error: string | null;
  chat_id: string | null;
  session_id: string | null;
  flow_id: string | null;
  flow_run_id: string | null;
}

export interface Schedule {
  id: string;
  title: string;
  plan: string;
  target_kind: ScheduleTargetKind;
  backend: string;
  model: string | null;
  flow_id: string | null;
  demo_task: string | null;
  status: ScheduleStatus;
  pause_reason: PauseReason;
  trigger: ScheduleTrigger;
  unread: boolean;
  created_at: number;
  updated_at: number;
  last_run_at: number | null;
  next_run_at: number | null;
  run_count: number;
  last_run: ScheduleRun | null;
  badge: string;
  trigger_summary: string;
  runs?: ScheduleRun[];
}

export interface SuggestedSchedule {
  id: string;
  title: string;
  description: string;
  target_kind: ScheduleTargetKind;
  backend: string;
  demo_task?: string;
  flow_id?: string;
  plan: string;
  trigger: Partial<ScheduleTrigger>;
  badge: string;
  trigger_summary: string;
}

export interface PermissionOperation {
  key: string;
  label: string;
  description: string;
  default: PermissionLevel;
  level: PermissionLevel;
}

export interface SamplePrompt {
  label: string;
  prompt: string;
}

export interface PermissionCategory {
  id: string;
  title: string;
  description: string;
  level: PermissionLevel | "custom";
  count: number;
  operations: PermissionOperation[];
  sample_prompts: SamplePrompt[];
}

export interface PermissionsResponse {
  categories: PermissionCategory[];
  values: Record<string, PermissionLevel>;
}

export interface ScheduleCatalog {
  suggested: SuggestedSchedule[];
  permissions: PermissionsResponse;
  flows: FlowSummary[];
  flow_templates: FlowTemplateSummary[];
  antigravity: {
    available: boolean;
    reason: string;
    default_model: string | null;
  };
}
