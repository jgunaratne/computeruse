# computeruse

An agent harness that operates a real computer through screenshots, mouse and keyboard, with a web console for watching, steering and measuring each run.

## How it works

```
Web console (React/TS) ──HTTP/WS──► API server + agent loop (FastAPI) ──HTTP──► control daemon in VM
        ▲   events, frames                │                                 (Xvfb + Chrome | GNOME)
        └─────────────────────────────────┤
                                          ▼
                         Model providers: Claude (Anthropic API / Vertex AI) · Gemini · Antigravity
```

- **Agent loop:** observe → think → act. Coordinates are scaled to model-friendly resolutions, with a harness-served `zoom` for small text, screen-settle detection, stuck detection that nudges before giving up, and step/time/cost budgets.
- **Computers:** a real Chrome on a private Xvfb display, the VM's live GNOME desktop (Wayland, via Mutter RemoteDesktop), a remote VM, a Docker sandbox, or a deterministic simulated desktop for tests.
- **Guardrails:** blocked key chords, domain allow/block lists, approval gates for credentials, card numbers and destructive shell commands, and granular per-operation tool permissions (*Always allow*, *Ask every time*, *Never allow*, *Custom*). Per-session overrides can only tighten them.
- **Operator console:** built on the **Craft Design System** (`web/public/vendor/craft-ds/`, 14 themes across Pro, Studio, and Classic grammars + 4 UI font scales + `?theme=`/`?font=` deep links), with live preview, a timeline with the model's reasoning, action overlays, frame scrubbing with `?step=N` deep links, pause/step/cancel, take-control, a Chat drawer that can supervise and drive the computer, a drag-and-drop **Agents** flow canvas, and a **Schedules** tab for recurring automations.
- **Evals:** YAML suites with verifiers, negative controls, replay of recorded sessions, and a CI pass-rate gate. Every event lands in SQLite, which feeds metrics like false-completion rate and p95 action latency.

See [ARCHITECTURE.md](ARCHITECTURE.md) for details.

## Stack

Python 3.11 · FastAPI · Pydantic · Anthropic SDK · React + TypeScript (Vite) · Craft Design System · SQLite · Xvfb / Mutter · Docker

## Run it

```bash
uv sync
(cd web && npm install && npm run build)
uv run computeruse doctor     # which computers/providers are usable here
uv run computeruse serve      # http://127.0.0.1:8787
```

In the console, run **Scripted demo → `browser-smoke/wikipedia_article`** to see a real Chrome session without an API key. To let a model drive, set `ANTHROPIC_API_KEY` (or configure Vertex/Gemini) and run:

```bash
uv run computeruse run -b browser -t "Find the population of Iceland on Wikipedia."
make eval                     # deterministic eval suite on the simulated desktop
```

Full setup, configuration and design notes: [GUIDE.md](GUIDE.md).
