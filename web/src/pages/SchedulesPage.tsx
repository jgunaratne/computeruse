import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Pill, Spinner, useToast } from "../components/ui";
import { Link, navigate, openChatWithPrompt } from "../hooks/useRoute";
import { classNames, fmtDuration, fmtRelative } from "../lib/format";
import type {
  Backend,
  PermissionCategory,
  PermissionLevel,
  Schedule,
  ScheduleCatalog,
  ScheduleFrequency,
  ScheduleStatus,
  ScheduleTargetKind,
  ScheduleTrigger,
  SuggestedSchedule,
} from "../types";

const DAY_LABELS = [
  { day: 0, short: "S", name: "Sun" },
  { day: 1, short: "M", name: "Mon" },
  { day: 2, short: "T", name: "Tue" },
  { day: 3, short: "W", name: "Wed" },
  { day: 4, short: "T", name: "Thu" },
  { day: 5, short: "F", name: "Fri" },
  { day: 6, short: "S", name: "Sat" },
];

const MINUTE_MARKS = [
  { value: 0, label: ":00 (top of the hour)" },
  { value: 15, label: ":15 past the hour" },
  { value: 30, label: ":30 past the hour" },
  { value: 45, label: ":45 past the hour" },
];

const PERMISSION_LABELS: Record<PermissionLevel, string> = {
  allow: "Always allow",
  ask: "Ask every time",
  deny: "Never allow",
};

function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  switch (n % 10) {
    case 1:
      return `${n}st`;
    case 2:
      return `${n}nd`;
    case 3:
      return `${n}rd`;
    default:
      return `${n}th`;
  }
}

function defaultTrigger(): ScheduleTrigger {
  return {
    frequency: "daily",
    minute_of_hour: 0,
    time_of_day: "09:00",
    days_of_week: [1, 2, 3, 4, 5],
    day_of_month: 1,
    run_date: null,
    condition: "",
    end_date: null,
  };
}

function previewTriggerSummary(t: ScheduleTrigger): string {
  let base = "";
  if (t.frequency === "hourly") {
    base = t.minute_of_hour === 0 ? "Hourly around the top of the hour" : `Hourly around the :${String(t.minute_of_hour).padStart(2, "0")}-min mark`;
  } else if (t.frequency === "daily") {
    base = `Daily around ${t.time_of_day}`;
  } else if (t.frequency === "weekly") {
    const days = [...new Set(t.days_of_week)].sort((a, b) => a - b);
    const key = days.join(",");
    if (key === "0,1,2,3,4,5,6") base = `Daily around ${t.time_of_day}`;
    else if (key === "1,2,3,4,5") base = `Weekly on weekdays around ${t.time_of_day}`;
    else if (key === "0,6") base = `Weekly on weekends around ${t.time_of_day}`;
    else {
      const names = days.map((d) => DAY_LABELS.find((x) => x.day === d)?.name ?? "").filter(Boolean);
      base = `Weekly on ${names.join(", ")} around ${t.time_of_day}`;
    }
  } else if (t.frequency === "monthly") {
    base = `Monthly on ${ordinal(t.day_of_month)} around ${t.time_of_day}`;
  } else {
    base = `Will run once on ${t.run_date || "next slot"} around ${t.time_of_day}`;
  }
  const cond = t.condition.trim();
  return cond ? `${base} · when ${cond}` : base;
}

function targetLabel(kind: ScheduleTargetKind): string {
  if (kind === "antigravity_chat") return "Antigravity chat";
  if (kind === "flow") return "Agent flow";
  return "Computer session";
}

function badgeTone(badge: string): "muted" | "ok" | "warn" | "bad" | "info" {
  if (badge === "Paused") return "warn";
  if (badge === "Completed") return "muted";
  if (badge === "Tracking") return "info";
  return "ok";
}

interface EditorState {
  mode: "create" | "edit";
  id?: string;
  title: string;
  plan: string;
  target_kind: ScheduleTargetKind;
  backend: string;
  flow_id: string;
  demo_task: string;
  status: ScheduleStatus;
  trigger: ScheduleTrigger;
  hasEndDate: boolean;
}

export function SchedulesPage({ scheduleId }: { scheduleId?: string }) {
  const toast = useToast();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [catalog, setCatalog] = useState<ScheduleCatalog | null>(null);
  const [backends, setBackends] = useState<Backend[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<"all" | ScheduleStatus>("all");
  const [quickPrompt, setQuickPrompt] = useState("");
  const [quickTarget, setQuickTarget] = useState<ScheduleTargetKind>("antigravity_chat");
  const [quickFrequency, setQuickFrequency] = useState<ScheduleFrequency>("daily");
  const [quickTime, setQuickTime] = useState("09:00");
  const [creatingQuick, setCreatingQuick] = useState(false);
  const [runningIds, setRunningIds] = useState<Record<string, boolean>>({});
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<Schedule | null>(null);
  const [savingEditor, setSavingEditor] = useState(false);
  const [openCategories, setOpenCategories] = useState<Record<string, boolean>>({
    browser: true,
    input: false,
    system: false,
    automation: false,
  });

  const refresh = useCallback(async () => {
    try {
      const [list, cat, bks] = await Promise.all([
        api.schedules(),
        api.scheduleCatalog(),
        api.backends(),
      ]);
      setSchedules(list);
      setCatalog(cat);
      setBackends(bks);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "bad");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Open side-pane editor when navigated to `/schedules/:id`.
  useEffect(() => {
    if (!scheduleId) {
      setSelectedDetail(null);
      return;
    }
    let alive = true;
    api
      .schedule(scheduleId)
      .then((detail) => {
        if (!alive) return;
        setSelectedDetail(detail);
        setEditor({
          mode: "edit",
          id: detail.id,
          title: detail.title,
          plan: detail.plan,
          target_kind: detail.target_kind,
          backend: detail.backend || "simulated",
          flow_id: detail.flow_id || "offline-demo",
          demo_task: detail.demo_task || "simulated-basics/calc_multiply",
          status: detail.status,
          trigger: { ...defaultTrigger(), ...detail.trigger },
          hasEndDate: Boolean(detail.trigger.end_date),
        });
        if (detail.unread) {
          void api.markScheduleRead(detail.id).then((updated) => {
            setSchedules((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
          });
        }
      })
      .catch((e) => {
        if (!alive) return;
        toast(e instanceof Error ? e.message : String(e), "bad");
        navigate("/schedules", true);
      });
    return () => {
      alive = false;
    };
  }, [scheduleId, toast]);

  const openNewEditor = (preset?: Partial<EditorState>) => {
    const trig = { ...defaultTrigger(), ...(preset?.trigger ?? {}) };
    setSelectedDetail(null);
    setEditor({
      mode: "create",
      title: preset?.title ?? "",
      plan: preset?.plan ?? "",
      target_kind: preset?.target_kind ?? "antigravity_chat",
      backend: preset?.backend ?? "simulated",
      flow_id: preset?.flow_id ?? "offline-demo",
      demo_task: preset?.demo_task ?? "simulated-basics/calc_multiply",
      status: "active",
      trigger: trig,
      hasEndDate: Boolean(trig.end_date),
    });
  };

  const closeEditor = () => {
    setEditor(null);
    setSelectedDetail(null);
    if (scheduleId) navigate("/schedules");
  };

  const onQuickCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    const plan = quickPrompt.trim();
    if (!plan || creatingQuick) return;
    setCreatingQuick(true);
    try {
      const firstLine = plan.split("\n")[0].trim();
      const title = firstLine.length > 64 ? `${firstLine.slice(0, 61)}…` : firstLine;
      const created = await api.createSchedule({
        title,
        plan,
        target_kind: quickTarget,
        backend: "simulated",
        demo_task: quickTarget === "session" ? "simulated-basics/calc_multiply" : null,
        flow_id: quickTarget === "flow" ? "offline-demo" : null,
        trigger: {
          ...defaultTrigger(),
          frequency: quickFrequency,
          time_of_day: quickTime,
        },
      });
      setQuickPrompt("");
      setSchedules((prev) => [created, ...prev]);
      toast(`Scheduled "${created.title}" (${created.trigger_summary})`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    } finally {
      setCreatingQuick(false);
    }
  };

  const onAddSuggested = async (s: SuggestedSchedule) => {
    try {
      const created = await api.createSchedule({
        title: s.title,
        plan: s.plan,
        target_kind: s.target_kind,
        backend: s.backend,
        demo_task: s.demo_task ?? null,
        flow_id: s.flow_id ?? null,
        trigger: s.trigger,
      });
      setSchedules((prev) => [created, ...prev]);
      toast(`Added schedule "${created.title}"`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  };

  const onSaveEditor = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editor || savingEditor) return;
    if (!editor.title.trim() || !editor.plan.trim()) {
      toast("Provide both a schedule name and a plan.", "bad");
      return;
    }
    setSavingEditor(true);
    const triggerPayload: ScheduleTrigger = {
      ...editor.trigger,
      end_date: editor.hasEndDate ? editor.trigger.end_date : null,
    };
    try {
      if (editor.mode === "create") {
        const created = await api.createSchedule({
          title: editor.title.trim(),
          plan: editor.plan.trim(),
          target_kind: editor.target_kind,
          backend: editor.backend,
          flow_id: editor.target_kind === "flow" ? editor.flow_id : null,
          demo_task: editor.target_kind === "session" ? editor.demo_task || null : null,
          trigger: triggerPayload,
          status: editor.status,
        });
        setSchedules((prev) => [created, ...prev]);
        toast(`Created schedule "${created.title}"`);
        closeEditor();
      } else if (editor.id) {
        const updated = await api.updateSchedule(editor.id, {
          title: editor.title.trim(),
          plan: editor.plan.trim(),
          target_kind: editor.target_kind,
          backend: editor.backend,
          flow_id: editor.target_kind === "flow" ? editor.flow_id : null,
          demo_task: editor.target_kind === "session" ? editor.demo_task || null : null,
          trigger: triggerPayload,
          status: editor.status,
        });
        setSchedules((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
        toast(`Saved "${updated.title}"`);
        closeEditor();
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    } finally {
      setSavingEditor(false);
    }
  };

  const onTogglePause = async (s: Schedule) => {
    const nextStatus = s.status === "active" ? "paused" : "active";
    try {
      const updated = await api.setScheduleStatus(s.id, nextStatus, nextStatus === "paused" ? "user_action" : undefined);
      setSchedules((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      if (selectedDetail?.id === updated.id) {
        setSelectedDetail((d) => (d ? { ...d, ...updated } : d));
      }
      toast(nextStatus === "paused" ? `Paused "${s.title}"` : `Resumed "${s.title}"`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  };

  const onRunNow = async (s: Schedule) => {
    if (runningIds[s.id]) return;
    setRunningIds((prev) => ({ ...prev, [s.id]: true }));
    try {
      const res = await api.runScheduleNow(s.id, true);
      setSchedules((prev) => prev.map((item) => (item.id === res.schedule.id ? res.schedule : item)));
      if (selectedDetail?.id === s.id) {
        const refreshed = await api.schedule(s.id);
        setSelectedDetail(refreshed);
      }
      if (res.run.status === "completed") {
        toast(`Completed "${s.title}": ${res.run.summary}`);
      } else {
        toast(`Run finished with status ${res.run.status}: ${res.run.error ?? res.run.summary}`, "bad");
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    } finally {
      setRunningIds((prev) => ({ ...prev, [s.id]: false }));
    }
  };

  const onDelete = async (s: Schedule) => {
    try {
      await api.deleteSchedule(s.id);
      setSchedules((prev) => prev.filter((item) => item.id !== s.id));
      if (editor?.id === s.id) closeEditor();
      toast(`Deleted "${s.title}"`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  };

  const onUpdatePermission = async (key: string, level: PermissionLevel) => {
    try {
      const updated = await api.updatePermissions({ updates: { [key]: level } });
      setCatalog((prev) => (prev ? { ...prev, permissions: updated } : prev));
      toast(`Updated ${key.split("::")[1]} to "${PERMISSION_LABELS[level]}"`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  };

  const onUpdateCategoryPermission = async (categoryId: string, level: PermissionLevel) => {
    try {
      const updated = await api.updatePermissions({ category_id: categoryId, level });
      setCatalog((prev) => (prev ? { ...prev, permissions: updated } : prev));
      toast(`Set category "${categoryId}" permissions to "${PERMISSION_LABELS[level]}"`);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  };

  const onResetPermissions = async () => {
    try {
      const updated = await api.updatePermissions({ reset: true });
      setCatalog((prev) => (prev ? { ...prev, permissions: updated } : prev));
      toast("Reset all operation permissions to defaults");
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err), "bad");
    }
  };

  const counts = useMemo(() => {
    const c = { all: schedules.length, active: 0, paused: 0, completed: 0 };
    for (const s of schedules) c[s.status] = (c[s.status] ?? 0) + 1;
    return c;
  }, [schedules]);

  const filtered = useMemo(
    () => (statusFilter === "all" ? schedules : schedules.filter((s) => s.status === statusFilter)),
    [schedules, statusFilter],
  );

  const activeList = useMemo(() => filtered.filter((s) => s.status === "active"), [filtered]);
  const pausedList = useMemo(() => filtered.filter((s) => s.status === "paused"), [filtered]);
  const completedList = useMemo(() => filtered.filter((s) => s.status === "completed"), [filtered]);

  return (
    <div className={`page wide fade-in schedule-page-wrap ${editor ? "with-editor" : ""}`}>
      <div className="schedule-main-col">
        <div className="page-head">
          <div>
            <h1>Schedules</h1>
            <p className="sub">
              Automate Antigravity prompts, recurring computer-use checks, and multi-agent flows on a schedule.
            </p>
          </div>
          <div className="row wrap">
            <button
              type="button"
              className="craft-btn craft-btn-secondary btn"
              onClick={() =>
                openChatWithPrompt(
                  "Help me design a recurring schedule for computer-use checks or daily Antigravity briefings.",
                )
              }
              title="Ask Chat to create or tune a schedule"
            >
              Ask in Chat ↗
            </button>
            <button type="button" className="craft-btn craft-btn-primary btn primary" onClick={() => openNewEditor()}>
              <span className="craft-icon craft-icon-sm" data-craft-icon="plus" aria-hidden="true" />
              New schedule
            </button>
          </div>
        </div>

        {/* Quick-Create Bar */}
        <form className="craft-panel card schedule-quick-bar" onSubmit={onQuickCreate}>
          <div className="schedule-quick-row">
            <input
              className="input schedule-quick-input"
              type="text"
              placeholder="Schedule a prompt or task (e.g. Call Antigravity every morning at 09:00 to review session outcomes)…"
              value={quickPrompt}
              onChange={(e) => setQuickPrompt(e.target.value)}
              aria-label="Quick schedule plan"
            />
            <select
              className="select schedule-quick-select"
              value={quickTarget}
              onChange={(e) => setQuickTarget(e.target.value as ScheduleTargetKind)}
              aria-label="Schedule target"
            >
              <option value="antigravity_chat">Antigravity chat</option>
              <option value="session">Computer session</option>
              <option value="flow">Agent flow</option>
            </select>
            <select
              className="select schedule-quick-freq"
              value={quickFrequency}
              onChange={(e) => setQuickFrequency(e.target.value as ScheduleFrequency)}
              aria-label="Schedule cadence"
            >
              <option value="hourly">Hourly</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekdays</option>
              <option value="monthly">Monthly</option>
              <option value="once">Once</option>
            </select>
            {quickFrequency !== "hourly" && (
              <input
                className="input schedule-quick-time"
                type="time"
                value={quickTime}
                onChange={(e) => setQuickTime(e.target.value || "09:00")}
                aria-label="Schedule time"
              />
            )}
            <button className="craft-btn craft-btn-primary btn primary" type="submit" disabled={!quickPrompt.trim() || creatingQuick}>
              {creatingQuick ? <Spinner /> : "Schedule"}
            </button>
          </div>
        </form>

        {/* Status filter pills */}
        <div className="schedule-toolbar">
          <div className="craft-tab-strip btn-group" role="tablist" aria-label="Schedule status filter">
            {(["all", "active", "paused", "completed"] as const).map((st) => (
              <button
                key={st}
                type="button"
                role="tab"
                aria-selected={statusFilter === st}
                className={classNames("craft-tab", statusFilter === st && "is-active toggled")}
                onClick={() => setStatusFilter(st)}
              >
                {st === "all" ? "All" : st.charAt(0).toUpperCase() + st.slice(1)}
                <span className="schedule-filter-count">{counts[st]}</span>
              </button>
            ))}
          </div>
          <span className="grow" />
          <span className="small dim">
            Background scheduler active · {counts.active} {counts.active === 1 ? "routine" : "routines"} armed
          </span>
        </div>

        {loading ? (
          <div className="craft-panel card pad">
            <div className="row">
              <Spinner /> <span className="muted">Loading schedules…</span>
            </div>
          </div>
        ) : filtered.length === 0 ? (
          <div className="craft-panel card schedule-empty">
            <strong>
              {statusFilter === "all"
                ? "No scheduled automations yet"
                : `No ${statusFilter} schedules`}
            </strong>
            <p className="muted small">
              Create a recurring Antigravity prompt, browser smoke test, or multi-agent verification flow using the bar above or a starter template below.
            </p>
            <div>
              <button type="button" className="craft-btn craft-btn-primary btn primary sm" onClick={() => openNewEditor()}>
                <span className="craft-icon craft-icon-sm" data-craft-icon="plus" aria-hidden="true" />
                Create first schedule
              </button>
            </div>
          </div>
        ) : (
          <div className="schedule-sections">
            {activeList.length > 0 && (
              <ScheduleSection
                title="Active"
                items={activeList}
                runningIds={runningIds}
                selectedId={editor?.id}
                onRunNow={onRunNow}
                onTogglePause={onTogglePause}
                onDelete={onDelete}
              />
            )}
            {pausedList.length > 0 && (
              <ScheduleSection
                title="Paused"
                items={pausedList}
                runningIds={runningIds}
                selectedId={editor?.id}
                onRunNow={onRunNow}
                onTogglePause={onTogglePause}
                onDelete={onDelete}
              />
            )}
            {completedList.length > 0 && (
              <ScheduleSection
                title="Completed"
                items={completedList}
                runningIds={runningIds}
                selectedId={editor?.id}
                onRunNow={onRunNow}
                onTogglePause={onTogglePause}
                onDelete={onDelete}
              />
            )}
          </div>
        )}

        {/* Suggested starter templates */}
        {statusFilter === "all" && catalog && catalog.suggested.length > 0 && (
          <section className="schedule-section">
            <div className="schedule-section-head">
              <h2>Suggested</h2>
              <span className="small dim">One-click routines for Antigravity, simulated desktop checks, and agent flows</span>
            </div>
            <div className="schedule-suggested-grid">
              {catalog.suggested.map((s) => (
                <div key={s.id} className="craft-panel card schedule-suggested-card">
                  <div className="row">
                    <span className="schedule-card-title">{s.title}</span>
                    <span className="grow" />
                    <Pill tone={badgeTone(s.badge)}>{s.badge}</Pill>
                  </div>
                  <div className="small muted">{s.description}</div>
                  <div className="schedule-meta-line">
                    <span className="tag">{targetLabel(s.target_kind)}</span>
                    <span className="small dim">{s.trigger_summary}</span>
                  </div>
                  <div className="row" style={{ marginTop: "auto", paddingTop: 6 }}>
                    <button type="button" className="craft-btn craft-btn-primary btn sm primary" onClick={() => void onAddSuggested(s)}>
                      <span className="craft-icon craft-icon-sm" data-craft-icon="plus" aria-hidden="true" />
                      Add schedule
                    </button>
                    <button
                      type="button"
                      className="craft-btn-compact btn sm ghost"
                      onClick={() =>
                        openNewEditor({
                          title: s.title,
                          plan: s.plan,
                          target_kind: s.target_kind,
                          backend: s.backend,
                          flow_id: s.flow_id ?? "offline-demo",
                          demo_task: s.demo_task ?? "simulated-basics/calc_multiply",
                          trigger: { ...defaultTrigger(), ...s.trigger },
                        })
                      }
                    >
                      Customize…
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Granular Per-Operation Tool Permissions & Sample Prompts */}
        {catalog && (
          <section className="schedule-section" aria-label="Tool permissions and sample prompts">
            <div className="schedule-section-head">
              <div>
                <h2>Action Permissions & Prompts to Try</h2>
                <p className="small dim" style={{ margin: "2px 0 0" }}>
                  Configure per-operation guardrails (Always allow, Ask every time, Never allow) or launch a sample prompt directly in Chat.
                </p>
              </div>
              <button type="button" className="craft-btn-compact btn sm ghost" onClick={() => void onResetPermissions()}>
                Reset defaults
              </button>
            </div>

            <div className="perm-categories">
              {catalog.permissions.categories.map((cat) => (
                <PermissionCategoryCard
                  key={cat.id}
                  category={cat}
                  open={Boolean(openCategories[cat.id])}
                  onToggle={() => setOpenCategories((prev) => ({ ...prev, [cat.id]: !prev[cat.id] }))}
                  onUpdateOperation={onUpdatePermission}
                  onUpdateCategory={onUpdateCategoryPermission}
                />
              ))}
            </div>
          </section>
        )}
      </div>

      {/* Animated Side-Pane Editor */}
      {editor && (
        <aside className="craft-panel schedule-side-pane" aria-label="Schedule editor">
          <div className="schedule-pane-head">
            <div>
              <strong>{editor.mode === "create" ? "New schedule" : "Edit schedule"}</strong>
              <div className="small dim">{previewTriggerSummary(editor.trigger)}</div>
            </div>
            <span className="grow" />
            <button
              type="button"
              className="craft-icon-btn craft-icon-btn-sm btn sm ghost icon"
              onClick={closeEditor}
              aria-label="Close schedule editor"
            >
              <span className="craft-icon craft-icon-sm" data-craft-icon="x" aria-hidden="true" />
            </button>
          </div>

          <form className="schedule-pane-body" onSubmit={onSaveEditor}>
            <label className="field">
              <span className="label craft-pro-colon">Name your schedule</span>
              <input
                className="input"
                type="text"
                placeholder="e.g. Morning Antigravity briefing"
                value={editor.title}
                onChange={(e) => setEditor({ ...editor, title: e.target.value })}
                required
              />
            </label>

            <div className="field">
              <span className="label craft-pro-colon">Target action</span>
              <div className="craft-tab-strip btn-group schedule-target-seg" role="tablist">
                {(["antigravity_chat", "session", "flow"] as const).map((tk) => (
                  <button
                    key={tk}
                    type="button"
                    role="tab"
                    aria-selected={editor.target_kind === tk}
                    className={classNames("craft-tab", editor.target_kind === tk && "is-active toggled")}
                    onClick={() => setEditor({ ...editor, target_kind: tk })}
                  >
                    {targetLabel(tk)}
                  </button>
                ))}
              </div>
            </div>

            {editor.target_kind === "session" && (
              <div className="grid-2">
                <label className="field">
                  <span className="label craft-pro-colon">Computer backend</span>
                  <select
                    className="select"
                    value={editor.backend}
                    onChange={(e) => setEditor({ ...editor, backend: e.target.value })}
                  >
                    {backends.map((b) => (
                      <option key={b.id} value={b.id}>
                        {b.label} {!b.available ? "(offline)" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="label craft-pro-colon">Reference script (optional)</span>
                  <select
                    className="select"
                    value={editor.demo_task}
                    onChange={(e) => setEditor({ ...editor, demo_task: e.target.value })}
                  >
                    <option value="">Live model execution</option>
                    <option value="simulated-basics/calc_multiply">SimOS · Calculator (12 × 7)</option>
                    <option value="simulated-basics/notes_type_save">SimOS · Notes checklist</option>
                  </select>
                </label>
              </div>
            )}

            {editor.target_kind === "flow" && (
              <label className="field">
                <span className="label craft-pro-colon">Multi-agent flow</span>
                <select
                  className="select"
                  value={editor.flow_id}
                  onChange={(e) => setEditor({ ...editor, flow_id: e.target.value })}
                >
                  <optgroup label="Starter flow patterns">
                    {(catalog?.flow_templates ?? []).map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </optgroup>
                  {(catalog?.flows ?? []).length > 0 && (
                    <optgroup label="Saved flows">
                      {catalog!.flows.map((f) => (
                        <option key={f.id} value={f.id}>
                          {f.name} ({f.nodes} nodes)
                        </option>
                      ))}
                    </optgroup>
                  )}
                </select>
              </label>
            )}

            <label className="field">
              <span className="label craft-pro-colon">
                {editor.target_kind === "antigravity_chat"
                  ? "Prompt sent to Antigravity"
                  : editor.target_kind === "flow"
                    ? "Flow goal override"
                    : "Task instruction for computer agent"}
              </span>
              <textarea
                className="textarea"
                rows={4}
                placeholder="Describe the plan…"
                value={editor.plan}
                onChange={(e) => setEditor({ ...editor, plan: e.target.value })}
                required
              />
            </label>

            {/* Frequency Card */}
            <div className="schedule-freq-card">
              <div className="schedule-freq-title">Frequency & Trigger</div>

              <div className="grid-2">
                <label className="field">
                  <span className="label craft-pro-colon">Repeat</span>
                  <select
                    className="select"
                    value={editor.trigger.frequency}
                    onChange={(e) =>
                      setEditor({
                        ...editor,
                        trigger: { ...editor.trigger, frequency: e.target.value as ScheduleFrequency },
                      })
                    }
                  >
                    <option value="hourly">Hourly</option>
                    <option value="daily">Daily</option>
                    <option value="weekly">Weekly</option>
                    <option value="monthly">Monthly</option>
                    <option value="once">Once</option>
                  </select>
                </label>

                {editor.trigger.frequency === "hourly" ? (
                  <label className="field">
                    <span className="label craft-pro-colon">Minute mark</span>
                    <select
                      className="select"
                      value={editor.trigger.minute_of_hour}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          trigger: { ...editor.trigger, minute_of_hour: Number(e.target.value) },
                        })
                      }
                    >
                      {MINUTE_MARKS.map((m) => (
                        <option key={m.value} value={m.value}>
                          {m.label}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : (
                  <label className="field">
                    <span className="label craft-pro-colon">Time of day</span>
                    <input
                      className="input"
                      type="time"
                      value={editor.trigger.time_of_day}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          trigger: { ...editor.trigger, time_of_day: e.target.value || "09:00" },
                        })
                      }
                    />
                  </label>
                )}
              </div>

              {editor.trigger.frequency === "weekly" && (
                <div className="field">
                  <div className="row">
                    <span className="label craft-pro-colon">Days of week</span>
                    <span className="grow" />
                    <button
                      type="button"
                      className="craft-btn-compact btn sm ghost"
                      onClick={() =>
                        setEditor({
                          ...editor,
                          trigger: { ...editor.trigger, days_of_week: [1, 2, 3, 4, 5] },
                        })
                      }
                    >
                      Weekdays
                    </button>
                    <button
                      type="button"
                      className="craft-btn-compact btn sm ghost"
                      onClick={() =>
                        setEditor({
                          ...editor,
                          trigger: { ...editor.trigger, days_of_week: [0, 6] },
                        })
                      }
                    >
                      Weekends
                    </button>
                  </div>
                  <div className="schedule-day-pills">
                    {DAY_LABELS.map((d) => {
                      const active = editor.trigger.days_of_week.includes(d.day);
                      return (
                        <button
                          key={d.day}
                          type="button"
                          className={`schedule-day-pill ${active ? "active" : ""}`}
                          title={d.name}
                          aria-pressed={active}
                          onClick={() => {
                            const cur = editor.trigger.days_of_week;
                            const next = active
                              ? cur.length > 1
                                ? cur.filter((x) => x !== d.day)
                                : cur
                              : [...cur, d.day].sort((a, b) => a - b);
                            setEditor({
                              ...editor,
                              trigger: { ...editor.trigger, days_of_week: next },
                            });
                          }}
                        >
                          {d.short}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {editor.trigger.frequency === "monthly" && (
                <label className="field">
                  <span className="label craft-pro-colon">Day of month</span>
                  <select
                    className="select"
                    value={editor.trigger.day_of_month}
                    onChange={(e) =>
                      setEditor({
                        ...editor,
                        trigger: { ...editor.trigger, day_of_month: Number(e.target.value) },
                      })
                    }
                  >
                    {Array.from({ length: 31 }, (_, i) => i + 1).map((day) => (
                      <option key={day} value={day}>
                        {ordinal(day)} of each month
                      </option>
                    ))}
                  </select>
                </label>
              )}

              {editor.trigger.frequency === "once" && (
                <label className="field">
                  <span className="label craft-pro-colon">Date</span>
                  <input
                    className="input"
                    type="date"
                    value={editor.trigger.run_date ?? ""}
                    onChange={(e) =>
                      setEditor({
                        ...editor,
                        trigger: { ...editor.trigger, run_date: e.target.value || null },
                      })
                    }
                  />
                </label>
              )}

              <label className="field">
                <span className="label craft-pro-colon">Tracking condition (optional)</span>
                <input
                  className="input"
                  type="text"
                  placeholder="e.g. Only alert when a check fails or layout changes"
                  value={editor.trigger.condition}
                  onChange={(e) =>
                    setEditor({
                      ...editor,
                      trigger: { ...editor.trigger, condition: e.target.value },
                    })
                  }
                />
              </label>

              {editor.trigger.frequency !== "once" && (
                <div className="field">
                  <span className="label craft-pro-colon">Ends</span>
                  <div className="row">
                    <select
                      className="select"
                      value={editor.hasEndDate ? "date" : "never"}
                      onChange={(e) => {
                        const has = e.target.value === "date";
                        setEditor({
                          ...editor,
                          hasEndDate: has,
                          trigger: { ...editor.trigger, end_date: has ? editor.trigger.end_date : null },
                        });
                      }}
                    >
                      <option value="never">Never</option>
                      <option value="date">On date</option>
                    </select>
                    {editor.hasEndDate && (
                      <input
                        className="input"
                        type="date"
                        value={editor.trigger.end_date ?? ""}
                        onChange={(e) =>
                          setEditor({
                            ...editor,
                            trigger: { ...editor.trigger, end_date: e.target.value || null },
                          })
                        }
                      />
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className="row" style={{ marginTop: 4 }}>
              <button className="craft-btn craft-btn-primary btn primary" type="submit" disabled={savingEditor}>
                {savingEditor ? <Spinner /> : editor.mode === "create" ? "Create schedule" : "Save changes"}
              </button>
              <button className="craft-btn craft-btn-secondary btn ghost" type="button" onClick={closeEditor}>
                Cancel
              </button>
            </div>

            {selectedDetail && selectedDetail.runs && selectedDetail.runs.length > 0 && (
              <div className="schedule-runs-box">
                <div className="schedule-freq-title">Recent runs ({selectedDetail.runs.length})</div>
                <div className="schedule-runs-list">
                  {selectedDetail.runs.map((r) => (
                    <div key={r.id} className="schedule-run-row">
                      <div className="row">
                        <Pill tone={r.status === "completed" ? "ok" : r.status === "failed" ? "bad" : "info"}>
                          {r.status}
                        </Pill>
                        <span className="small dim">{fmtRelative(r.started_at)}</span>
                        {r.duration_ms != null && <span className="small dim">· {fmtDuration(r.duration_ms)}</span>}
                        <span className="grow" />
                        {r.session_id && (
                          <Link to={`/sessions/${r.session_id}`} className="craft-btn-compact btn sm ghost">
                            Go to session ↗
                          </Link>
                        )}
                        {r.flow_id && (
                          <Link to={`/agents/${r.flow_id}`} className="craft-btn-compact btn sm ghost">
                            Go to flow ↗
                          </Link>
                        )}
                      </div>
                      <div className="small muted">{r.error ?? r.summary}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </form>
        </aside>
      )}
    </div>
  );
}

function ScheduleSection({
  title,
  items,
  runningIds,
  selectedId,
  onRunNow,
  onTogglePause,
  onDelete,
}: {
  title: string;
  items: Schedule[];
  runningIds: Record<string, boolean>;
  selectedId?: string;
  onRunNow: (s: Schedule) => void;
  onTogglePause: (s: Schedule) => void;
  onDelete: (s: Schedule) => void;
}) {
  return (
    <section className="schedule-section">
      <div className="schedule-section-head">
        <h2>{title}</h2>
        <span className="small dim">{items.length}</span>
      </div>
      <div className="schedule-list">
        {items.map((s) => {
          const running = Boolean(runningIds[s.id]);
          const lastRunStatus = s.last_run_at ? `Last run ${fmtRelative(s.last_run_at)}` : "Waiting to run";
          const subtitle = `${s.trigger_summary} · ${lastRunStatus}`;
          return (
            <div
              key={s.id}
              className={classNames("craft-panel card schedule-row-card", selectedId === s.id && "selected")}
            >
              <div className="schedule-row-main">
                <div className="row wrap">
                  {s.unread && (
                    <span
                      className="schedule-unread-dot"
                      title="New run completed since you last viewed this schedule"
                    />
                  )}
                  <Link to={`/schedules/${s.id}`} className="schedule-card-title">
                    {s.title}
                  </Link>
                  <span className="tag">{targetLabel(s.target_kind)}</span>
                  <Pill tone={badgeTone(s.badge)}>{s.badge}</Pill>
                </div>

                <div className="schedule-subtitle" title={subtitle}>
                  {subtitle}
                </div>

                {s.pause_reason === "execution_failed" && s.status === "paused" && (
                  <div className="small text-warn">
                    Paused after a failed run — review the plan and resume when ready.
                  </div>
                )}

                {s.last_run && (
                  <div className="schedule-last-run">
                    <span
                      className={`schedule-run-dot ${s.last_run.status}`}
                      aria-hidden
                    />
                    <span className="small dim">{s.last_run.summary}</span>
                    {s.last_run.session_id && (
                      <Link to={`/sessions/${s.last_run.session_id}`} className="schedule-jump-link">
                        Session {s.last_run.session_id.slice(0, 8)} ↗
                      </Link>
                    )}
                    {s.last_run.flow_id && (
                      <Link to={`/agents/${s.last_run.flow_id}`} className="schedule-jump-link">
                        Flow ↗
                      </Link>
                    )}
                  </div>
                )}
              </div>

              <div className="schedule-row-actions">
                <button
                  type="button"
                  className="craft-btn craft-btn-secondary btn sm"
                  onClick={() => void onRunNow(s)}
                  disabled={running}
                  title="Execute this schedule immediately"
                >
                  {running ? (
                    <Spinner />
                  ) : (
                    <>
                      <span className="craft-icon craft-icon-sm" data-craft-icon="play" aria-hidden="true" />
                      Run now
                    </>
                  )}
                </button>
                {s.status !== "completed" && (
                  <button
                    type="button"
                    className="craft-btn-compact btn sm ghost"
                    onClick={() => void onTogglePause(s)}
                    title={s.status === "active" ? "Pause schedule" : "Resume schedule"}
                  >
                    {s.status === "active" ? "Pause" : "Resume"}
                  </button>
                )}
                <Link to={`/schedules/${s.id}`} className="craft-btn-compact btn sm ghost">
                  Edit
                </Link>
                <button
                  type="button"
                  className="craft-icon-btn craft-icon-btn-sm btn sm ghost icon"
                  onClick={() => void onDelete(s)}
                  aria-label={`Delete ${s.title}`}
                  title="Delete schedule"
                >
                  <span className="craft-icon craft-icon-sm" data-craft-icon="trash" aria-hidden="true" />
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function PermissionCategoryCard({
  category,
  open,
  onToggle,
  onUpdateOperation,
  onUpdateCategory,
}: {
  category: PermissionCategory;
  open: boolean;
  onToggle: () => void;
  onUpdateOperation: (key: string, level: PermissionLevel) => void;
  onUpdateCategory: (categoryId: string, level: PermissionLevel) => void;
}) {
  return (
    <div className="craft-panel card perm-card">
      <div className="perm-card-head">
        <button
          type="button"
          className="perm-card-toggle"
          onClick={onToggle}
          aria-expanded={open}
        >
          <span
            key={open ? "open" : "closed"}
            className="craft-icon craft-icon-sm perm-chevron"
            data-craft-icon={open ? "chevron-down" : "chevron-right"}
            aria-hidden="true"
          />
          <div>
            <div className="row">
              <strong>{category.title}</strong>
              <span className="schedule-filter-count">{category.count}</span>
            </div>
            <div className="small dim">{category.description}</div>
          </div>
        </button>

        <div className="perm-bulk-wrap">
          <select
            className={`select perm-pill perm-${category.level}`}
            value={category.level}
            aria-label={`${category.title} category permission`}
            onChange={(e) => {
              const val = e.target.value;
              if (val === "allow" || val === "ask" || val === "deny") {
                onUpdateCategory(category.id, val);
              }
            }}
          >
            {category.level === "custom" && (
              <option value="custom" disabled>
                ⋯ Custom
              </option>
            )}
            <option value="allow">Always allow</option>
            <option value="ask">Ask every time</option>
            <option value="deny">Never allow</option>
          </select>
        </div>
      </div>

      {open && (
        <div className="perm-card-body">
          <div className="perm-ops-list">
            {category.operations.map((op) => (
              <div key={op.key} className="perm-op-row">
                <div>
                  <div className="perm-op-label">{op.label}</div>
                  <div className="small dim">{op.description}</div>
                </div>
                <select
                  className={`select perm-pill perm-${op.level}`}
                  value={op.level}
                  aria-label={`${op.label} permission`}
                  onChange={(e) => onUpdateOperation(op.key, e.target.value as PermissionLevel)}
                >
                  <option value="allow">Always allow</option>
                  <option value="ask">Ask every time</option>
                  <option value="deny">Never allow</option>
                </select>
              </div>
            ))}
          </div>

          {category.sample_prompts.length > 0 && (
            <div className="perm-prompts">
              <span className="small dim">Prompts to try in Chat:</span>
              <div className="perm-prompt-chips">
                {category.sample_prompts.map((sp, i) => (
                  <button
                    key={i}
                    type="button"
                    className="perm-prompt-chip"
                    onClick={() => openChatWithPrompt(sp.prompt)}
                    title={sp.prompt}
                  >
                    <span>{sp.label}</span>
                    <span className="perm-prompt-arrow" aria-hidden>
                      ↗
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
