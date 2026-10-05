import { useEffect, useState } from "react";
import { api } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { ToastProvider, useAsync } from "./components/ui";
import { Link, useRoute } from "./hooks/useRoute";
import { EvalsPage } from "./pages/EvalsPage";
import { MetricsPage } from "./pages/MetricsPage";
import { SessionPage } from "./pages/SessionPage";
import { SessionsPage } from "./pages/SessionsPage";

const CHAT_OPEN_KEY = "cu:chat:open";

export default function App() {
  const [route] = useRoute();
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
  useEffect(() => {
    try {
      localStorage.setItem(CHAT_OPEN_KEY, chatOpen ? "1" : "0");
    } catch {
      /* private mode */
    }
  }, [chatOpen]);
  return (
    <ToastProvider>
      <div className={`app ${chatOpen ? "with-chat" : ""}`}>
        <header className="topbar">
          <Link to="/" className="brand">
            <span className="logo" aria-hidden>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                <rect x="3" y="4" width="18" height="12" rx="2" />
                <path d="M8 20h8" />
              </svg>
            </span>
            computeruse
          </Link>
          <nav className="nav" aria-label="Primary">
            <Link to="/" className={route.name === "sessions" || route.name === "session" ? "active" : ""}>
              Sessions
            </Link>
            <Link to="/metrics" className={route.name === "metrics" ? "active" : ""}>
              Metrics
            </Link>
            <Link to="/evals" className={route.name === "evals" ? "active" : ""}>
              Evals
            </Link>
          </nav>
          <span className="spacer" />
          <div className="env" title="Computers the server can drive right now">
            {available.length > 0 && <span>computers: {available.join(", ")}</span>}
            {config.data && (
              <span
                className={`pill ${config.data.models_available ? "ok" : "warn"}`}
                title={
                  config.data.models_available
                    ? Object.entries(config.data.model_access.providers).map(([p, st]) => `${p}: ${st.reason}`).join("\n")
                    : "No model provider configured: scripted/replay only"
                }
              >
                {config.data.models_available
                  ? `${config.data.models.find((m) => m.id === config.data!.model)?.provider ?? "model"} · ${config.data.model}`
                  : "no model access"}
              </span>
            )}
            <a className="dim" href="/api/docs" target="_blank" rel="noreferrer" title="OpenAPI docs">
              API
            </a>
            <button
              className={`btn sm ghost chat-toggle ${chatOpen ? "toggled" : ""}`}
              onClick={() => setChatOpen((v) => !v)}
              aria-pressed={chatOpen}
              title="Chat with an Antigravity model about the agent or the session on screen"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z" />
              </svg>
              Chat
            </button>
          </div>
        </header>
        <div className="body">
          <main>
            {route.name === "sessions" && <SessionsPage />}
            {route.name === "session" && <SessionPage key={route.id} id={route.id} />}
            {route.name === "metrics" && <MetricsPage />}
            {route.name === "evals" && <EvalsPage runId={route.runId} />}
          </main>
          {chatOpen && <ChatPanel sessionId={route.name === "session" ? route.id : undefined} onClose={() => setChatOpen(false)} />}
        </div>
      </div>
    </ToastProvider>
  );
}
