import { useEffect, useState } from "react";
import { api } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { ToastProvider, useAsync } from "./components/ui";
import { CHAT_PREFILL_EVENT, Link, useRoute } from "./hooks/useRoute";
import {
  CRAFT_FONT_SIZES,
  CRAFT_THEME_GROUPS,
  useCraftAppearance,
  type CraftThemeId,
} from "./lib/appearance";
import { classNames } from "./lib/format";
import { AgentsPage } from "./pages/AgentsPage";
import { EvalsPage } from "./pages/EvalsPage";
import { MetricsPage } from "./pages/MetricsPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { SessionPage } from "./pages/SessionPage";
import { SessionsPage } from "./pages/SessionsPage";

const CHAT_OPEN_KEY = "cu:chat:open";

export function App() {
  const [route] = useRoute();
  const { theme, fontSize, setTheme, setFontSize } = useCraftAppearance();
  const config = useAsync(() => api.config(), []);
  const backends = useAsync(() => api.backends(), []);
  const available = (backends.data ?? []).filter((b) => b.available).map((b) => b.id);
  const [chatOpen, setChatOpen] = useState(() => {
    try {
      return localStorage.getItem(CHAT_OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [chatPrefill, setChatPrefill] = useState<{ prompt: string; seq: number } | null>(null);
  useEffect(() => {
    try {
      localStorage.setItem(CHAT_OPEN_KEY, chatOpen ? "1" : "0");
    } catch {
      /* private mode */
    }
  }, [chatOpen]);
  useEffect(() => {
    const onPrefill = (e: Event) => {
      const detail = (e as CustomEvent<{ prompt?: string }>).detail;
      if (detail?.prompt) {
        setChatOpen(true);
        setChatPrefill({ prompt: detail.prompt, seq: Date.now() });
      }
    };
    window.addEventListener(CHAT_PREFILL_EVENT, onPrefill);
    return () => window.removeEventListener(CHAT_PREFILL_EVENT, onPrefill);
  }, []);

  return (
    <ToastProvider>
      <div className={classNames("app", chatOpen && "with-chat", route.name === "agents" && "is-canvas-route")}>
        <aside className="sidebar">
          <Link to="/" className="brand">
            <span className="logo" aria-hidden="true">
              <span className="craft-icon" data-craft-icon="mouse-pointer-2" />
            </span>
            <span>computeruse</span>
          </Link>
          <nav className="nav" aria-label="Primary">
            <Link to="/" className={route.name === "sessions" || route.name === "session" ? "active" : ""}>
              <span className="craft-icon nav-icon" data-craft-icon="monitor" aria-hidden="true" />
              <span>Sessions</span>
            </Link>
            <Link to="/agents" className={route.name === "agents" ? "active" : ""}>
              <span className="craft-icon nav-icon" data-craft-icon="spline" aria-hidden="true" />
              <span>Agents</span>
            </Link>
            <Link to="/schedules" className={route.name === "schedules" ? "active" : ""}>
              <span className="craft-icon nav-icon" data-craft-icon="clock" aria-hidden="true" />
              <span>Schedules</span>
            </Link>
            <Link to="/metrics" className={route.name === "metrics" ? "active" : ""}>
              <span className="craft-icon nav-icon" data-craft-icon="gauge" aria-hidden="true" />
              <span>Metrics</span>
            </Link>
            <Link to="/evals" className={route.name === "evals" ? "active" : ""}>
              <span className="craft-icon nav-icon" data-craft-icon="check" aria-hidden="true" />
              <span>Evals</span>
            </Link>
          </nav>
          <span className="divider" aria-hidden="true" />
          <button
            type="button"
            className={classNames("chat-toggle", chatOpen && "toggled")}
            onClick={() => setChatOpen((v) => !v)}
            aria-pressed={chatOpen}
            title="Chat with an Antigravity model about the agent or the session on screen"
          >
            <span className="craft-icon nav-icon" data-craft-icon="message-square" aria-hidden="true" />
            <span>Chat</span>
          </button>
          <span className="spacer" />
          <div className="appearance-box" aria-label="Appearance">
            <label className="appearance-row">
              <span className="appearance-label craft-pro-colon">Theme</span>
              <select
                className="select sm-select appearance-select"
                aria-label="Theme"
                value={theme}
                onChange={(e) => setTheme(e.target.value as CraftThemeId)}
              >
                {CRAFT_THEME_GROUPS.map((g) => (
                  <optgroup key={g.grammar} label={g.grammar}>
                    {g.items.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <div className="appearance-row">
              <span className="appearance-label craft-pro-colon">Scale</span>
              <div className="font-scale-strip" role="group" aria-label="UI font size">
                {CRAFT_FONT_SIZES.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className={classNames("craft-btn-compact", "font-scale-btn", fontSize === f.id && "is-active")}
                    onClick={() => setFontSize(f.id)}
                    title={f.label}
                    aria-label={`Font size ${f.label}`}
                    aria-pressed={fontSize === f.id}
                  >
                    {f.short}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="env" title="Computers the server can drive right now">
            {available.length > 0 && <span className="computers">computers: {available.join(", ")}</span>}
            {config.data && (
              <span
                className={classNames("pill", config.data.models_available ? "ok" : "warn")}
                title={
                  config.data.models_available
                    ? Object.entries(config.data.model_access.providers)
                        .map(([p, st]) => `${p}: ${st.reason}`)
                        .join("\n")
                    : "No model provider configured: scripted/replay only"
                }
              >
                {config.data.models_available
                  ? `${config.data.models.find((m) => m.id === config.data!.model)?.provider ?? "model"} · ${config.data.model}`
                  : "no model access"}
              </span>
            )}
            <a className="dim" href="/api/docs" target="_blank" rel="noreferrer" title="OpenAPI docs">
              API docs ↗
            </a>
          </div>
        </aside>
        <div className="body">
          <main>
            {route.name === "sessions" && <SessionsPage />}
            {route.name === "session" && <SessionPage key={route.id} id={route.id} />}
            {route.name === "agents" && <AgentsPage flowId={route.id} />}
            {route.name === "schedules" && <SchedulesPage scheduleId={route.id} />}
            {route.name === "metrics" && <MetricsPage />}
            {route.name === "evals" && <EvalsPage runId={route.runId} />}
          </main>
          {chatOpen && (
            <ChatPanel
              sessionId={route.name === "session" ? route.id : undefined}
              prefill={chatPrefill}
              onClose={() => setChatOpen(false)}
            />
          )}
        </div>
      </div>
    </ToastProvider>
  );
}

export default App;
