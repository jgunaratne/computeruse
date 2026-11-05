# computeruse — design

`computeruse` is built on the **Craft Design System** (the PhotoCraft look), vendored read-only at `web/public/vendor/craft-ds/` (v0.16.0). This file describes how *this project* maps its operator console onto the kit; the design system itself is specified in `web/public/vendor/craft-ds/DESIGN.md` and demonstrated in `web/public/vendor/craft-ds/index.html`.

## Themes this project ships

| Theme id | Grammar | Where it is used |
|---|---|---|
| `proMedium` | `pro` (`pro=true`, `dark=true`) | Default console theme |
| `pro` | `pro` (`pro=true`, `dark=true`) | High-contrast dark Pro Spectrum option |
| `proLight` | `pro` (`pro=true`, `dark=false`) | Light Pro Spectrum option |
| `studio` | `studio` (`round_chips=true`, `dark=true`) | Modern rounded Studio dark option |
| `studioLight` | `studio` (`round_chips=true`, `dark=false`) | Modern rounded Studio light option |
| `classic` | `classic` (`bevel=true`, `dark=false`) | Classic 3D raised/sunken bevel desktop option |

The project verifies `proMedium`, `pro`, `proLight`, `studio`, `studioLight`, and `classic` at `1440` and `820` px wide across `small` and `large` UI font sizes (see `craft-ds.json` and `scripts/craft_shoot.py`). All 14 kit themes are selectable in the sidebar's Appearance picker (`web/src/lib/appearance.ts`) and via `?theme=<id>&font=<size>` query parameters.

<details><summary>All 14 kit themes and their grammar flags (v0.16.0)</summary>

| id | label | grammar | flags |
|---|---|---|---|
| `pro` | Pro (Dark) | pro | pro=true bevel=false round_chips=false dark=true |
| `proMedium` | Pro (Medium Gray) | pro | pro=true bevel=false round_chips=false dark=true |
| `proLightGray` | Pro (Light Gray) | pro | pro=true bevel=false round_chips=false dark=false |
| `proLight` | Pro (Light) | pro | pro=true bevel=false round_chips=false dark=false |
| `studio` | Studio (Dark) | studio | pro=false bevel=false round_chips=true dark=true |
| `studioLight` | Studio (Light) | studio | pro=false bevel=false round_chips=true dark=false |
| `classic` | Classic | classic | pro=false bevel=true round_chips=false dark=false |
| `solarizedDark` | Solarized Dark | studio | pro=false bevel=false round_chips=true dark=true |
| `adwaita` | Adwaita (Light) | studio | pro=false bevel=false round_chips=true dark=false |
| `adwaitaDark` | Adwaita (Dark) | studio | pro=false bevel=false round_chips=true dark=true |
| `breezeLight` | Breeze Light | studio | pro=false bevel=false round_chips=true dark=false |
| `breezeDark` | Breeze Dark | studio | pro=false bevel=false round_chips=true dark=true |
| `softDark` | Soft Dark | studio | pro=false bevel=false round_chips=true dark=true |
| `softLight` | Soft Light | studio | pro=false bevel=false round_chips=true dark=false |

</details>

## Project tokens

Project-specific semantic values use the `--cu-*` prefix in `web/src/styles.css` and derive exclusively from `--craft-*` tokens (switching foreground label swatches between `--craft-label-*` on `[data-craft-dark="true"]` and `--craft-label-*-eye` on `[data-craft-dark="false"]` for WCAG AA contrast):

| Token | Value | Purpose |
|---|---|---|
| `--sidebar-w` | `236px` | Left chrome navigation dock width on wide viewports |
| `--topbar-h` | `0px` (`52px` at `<= 900px`) | Sticky top offset when the dock collapses into a top bar |
| `--cu-chrome` / `--cu-page` / `--cu-card` / `--cu-well` | `var(--craft-chrome)` / `var(--craft-dock)` / `var(--craft-card)` / `var(--craft-field)` | Four-step surface hierarchy (rails -> page -> panels -> field wells) |
| `--cu-text` / `--cu-text-2` / `--cu-text-faint` | `var(--craft-text)` / `var(--craft-text-dim)` / `var(--craft-text-faint)` | Primary copy, AA-compliant secondary copy (`--craft-text-dim`), and placeholder-only faint ink |
| `--cu-ok` | `var(--craft-label-green)` / `var(--craft-label-green-eye)` | Verified / completed / pass status foreground |
| `--cu-ok-soft` | `color-mix(in srgb, var(--craft-label-green[-eye]) 14-16%, transparent)` | Soft tint behind completed / pass badges and banners |
| `--cu-warn` | `var(--craft-warning)` / `var(--craft-label-orange-eye)` | Paused / approval-needed / stuck warning foreground |
| `--cu-warn-soft` | `color-mix(in srgb, var(--craft-warning / --craft-label-orange-eye) 14-16%, transparent)` | Soft tint behind warning pills and approval banners |
| `--cu-bad` | `var(--craft-danger)` / `var(--craft-label-red-eye)` | Failed / error / guardrail-blocked foreground |
| `--cu-bad-soft` | `color-mix(in srgb, var(--craft-danger / --craft-label-red-eye) 14-16%, transparent)` | Soft tint behind error notices and failed steps |
| `--cu-info` | `var(--craft-label-blue)` / `var(--craft-accent-text)` (`var(--craft-accent)` in Classic bevel) | Operator takeover, live running state, and user actions |
| `--cu-marker` | `var(--craft-accent)` | Screen action-overlay ring, vector, and coordinate label |
| `--cu-port-text` | `var(--craft-label-yellow)` / `var(--craft-label-yellow-eye)` | Text-typed port dot and cubic wire stroke in Agent flows |
| `--cu-port-screen` | `var(--craft-label-seafoam)` / `var(--craft-label-seafoam-eye)` | Screen-typed port dot and cubic wire stroke in Agent flows |
| `--cu-node-computer` … `--cu-node-journal` | `var(--craft-label-*)` / `var(--craft-label-*-eye)` | Role indicator tint per flow node type (`computer`, `model`, `critic`, `join`, `journal`) |

## Screens and components

| Screen / component | Built from | Project class | File |
|---|---|---|---|
| App shell & navigation dock | `.craft-app`, `.craft-icon`, `.craft-btn-compact`, `.craft-pro-colon` | `.app`, `.sidebar`, `.nav`, `.appearance-box`, `.env`, `.body` | `web/src/App.tsx` |
| Shared status pills, toggles & toasts | `.craft-checkbox`, `.craft-toggle.is-adaptive`, `.craft-check-row`, `.craft-icon-btn`, `.craft-panel` | `.pill`, `.check`, `.toasts`, `.toast` | `web/src/components/ui.tsx` |
| Sessions list & New Task form | `.craft-panel`, `.craft-tab-strip`, `.craft-tab`, `.craft-btn`, `.craft-btn-primary`, `.craft-btn-secondary`, `.craft-btn-compact`, `.craft-pro-colon` | `.page`, `.card`, `.form-grid`, `.table` | `web/src/pages/SessionsPage.tsx`, `web/src/components/NewSessionForm.tsx`, `web/src/components/ModelSelect.tsx` |
| Live Session viewer, Screen & Timeline | `.craft-panel`, `.craft-tab-strip`, `.craft-tab`, `.craft-btn`, `.craft-btn-primary`, `.craft-btn-secondary`, `.craft-btn-danger`, `.craft-btn-compact`, `.craft-icon-btn` | `.session-layout`, `.screen-card`, `.screen`, `.controls`, `.timeline`, `.turn`, `.step` | `web/src/pages/SessionPage.tsx`, `web/src/components/ScreenView.tsx`, `web/src/components/Controls.tsx`, `web/src/components/Timeline.tsx` |
| Antigravity Chat dock | `.craft-btn`, `.craft-btn-primary`, `.craft-btn-danger`, `.craft-btn-compact`, `.craft-icon-btn`, `.craft-check-row`, `.craft-toggle.is-adaptive` | `.chat-panel`, `.chat-head`, `.chat-messages`, `.chat-composer`, `.msg` | `web/src/components/ChatPanel.tsx` |
| Agents (flow editor, canvas & inspector) | `.craft-panel`, `.craft-tab-strip`, `.craft-tab`, `.craft-menu`, `.craft-menu-item`, `.craft-menu-sep`, `.craft-btn`, `.craft-btn-primary`, `.craft-btn-danger`, `.craft-btn-compact`, `.craft-icon-btn`, `.craft-pro-colon` | `.flow-page`, `.flow-toolbar`, `.flow-palette`, `.flow-canvas`, `.flow-node`, `.flow-inspector` | `web/src/pages/AgentsPage.tsx`, `web/src/flow/FlowCanvas.tsx`, `web/src/flow/FlowInspector.tsx` |
| Schedules & Permissions | `.craft-panel`, `.craft-tab-strip`, `.craft-tab`, `.craft-btn`, `.craft-btn-primary`, `.craft-btn-secondary`, `.craft-btn-compact`, `.craft-icon-btn`, `.craft-pro-colon` | `.schedule-page-wrap`, `.schedule-suggested-card`, `.schedule-row-card`, `.schedule-side-pane`, `.perm-card` | `web/src/pages/SchedulesPage.tsx` |
| Metrics & Evals | `.craft-panel`, `.craft-btn`, `.craft-btn-primary`, `.craft-btn-compact`, `.craft-check-row`, `.craft-toggle.is-adaptive`, `.craft-pro-colon` | `.stats`, `.stat`, `.bars`, `.trend`, `.table`, `details.suite` | `web/src/pages/MetricsPage.tsx`, `web/src/pages/EvalsPage.tsx` |

## Copy and voice

User-facing strings live directly in `web/src/pages/*.tsx`, `web/src/components/*.tsx`, and `web/src/flow/*.tsx`. Labels are short, direct, and tool-oriented ("Run flow", "Take control", "Hand back & resume", "New schedule"). Form and inspector field labels append `.craft-pro-colon` so they render a trailing colon in Pro Spectrum (`Model:`, `Computer:`) and omit it in Studio and Classic (`Model`, `Computer`).

## Deliberate deviations

Every intentional difference from the kit showcase or from PhotoCraft, with the decision in `docs/DECISIONS.md` that records why.

| What differs | From | Why | Decision |
|---|---|---|---|
| Scrollable multi-page web console shell (`.app` with collapsible left `.sidebar`) | Fixed `100vh` `overflow: hidden` desktop image-editor shell | Sessions tables, Metrics charts, Evals suites, and Schedules lists hold unbounded records and collapse cleanly at `820px` width without horizontal scrolling (`Agents` flow editor still uses a locked viewport workspace) | Decision #2 |
| Native `<select>`, `<input>`, and `<textarea>` controls styled with `--craft-field*` tokens and `[data-craft-bevel="true"]` bevels | Div-based `.craft-dropdown` and `data-craft-numeric` `.craft-value-field` | Model and demo pickers require `<optgroup>` keyboard type-ahead, disabled option reasons, native `date`/`time`/`number` semantics, and form validation with zero third-party JS dependencies; drag-scrubbing integer step/cost caps would risk accidental budget changes | Decision #3 |
| Domain data-visualisation `<svg>` surfaces (`ScreenView` coordinate markers and `FlowCanvas` cubic patch-bay wires) | `.craft-icon` UI iconography | Action click/drag rings and node-graph cubic bezier wires are live coordinate-space graphics driven by physical display dimensions and graph geometry (`web/src/flow/graph.ts`), whereas all UI chrome icons use `[data-craft-icon]` | Decision #4 |
| Fixed `224px` node-card geometry (`HEADER_H = 40`, `PORT_ROW = 24`) on `FlowCanvas` | Scaling every inner row height with `--craft-font-scale` | `web/src/flow/graph.ts` computes wire endpoints deterministically without DOM measurement so SVG wires and hit-targets always land on port centres across zoom levels and font scales | Decision #5 |
