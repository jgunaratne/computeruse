# Decisions (computeruse)

Numbered, append-only. Context / Decision / Consequences. Read before re-deciding anything about the UI; link each "Deliberate deviations" row in `docs/DESIGN.md` to an entry here.

## 1. Build on the Craft Design System

**Context.** `computeruse` needs a dense, themeable desktop-tool UI for supervising live computer-use sessions, multi-agent flows, schedules, metrics, and evals, and should share visual grammar and tokens with PhotoCraft and sibling tools.

**Decision.** Vendor the Craft DS kit (`web/public/vendor/craft-ds/`, v0.16.0) read-only and hash-pinned; serve it from Vite's `public/` directory so both `make dev` and FastAPI's web/dist static mount serve `/vendor/craft-ds/...`; verify `proMedium`, `pro`, `proLight`, `studio`, `studioLight`, and `classic` across `small` and `large` UI font sizes at `1440` and `820` px wide; run `craft-ds check` and `craft-ds probe` alongside `make check` and `make ui-check`.

**Consequences.** Zero raw colour, radius, or font-family literals in `web/index.html` or `web/src/**`; zero `[data-craft-theme=…]` patches, `--craft-*` redefinitions, or `.craft-*` restyling selectors; kit upgrades happen atomically via `craft-ds update .`.

## 2. Responsive console shell with a locked-viewport flow workspace

**Context.** PhotoCraft is a single-canvas image editor (`height: 100vh; overflow: hidden`), whereas `computeruse` combines a full-viewport node-graph workspace (`/agents/<id>`) with scrollable operational tables and dashboards (`Sessions`, `Schedules`, `Metrics`, `Evals`) that must also pass narrow-viewport (`820×900`) checks without horizontal overflow.

**Decision.** Style `.craft-app` in `computeruse` as a token-driven two-pane shell (`--craft-chrome` navigation dock + `--craft-canvas` workspace + optional `--craft-dock` chat/inspector side panes) that scrolls vertically on document pages, locks the viewport on the `/agents/<id>` patch-bay canvas, and collapses the left dock into a `52px` `--craft-chrome` top bar at `<= 900px`.

**Consequences.** Every route renders in authentic Craft DS chrome and canvas tokens while remaining scrollable on long session/eval histories and free of horizontal scrollbars at `820px`.

## 3. Native form controls styled with Craft field tokens and Classic bevels

**Context.** `ModelSelect`, `NewSessionForm`, `SchedulesPage`, and `FlowInspector` rely on native `<select>` (with `<optgroup>` provider sections and disabled-reason tooltips), `<input type="number" | "time" | "date">`, and `<textarea>` form semantics, and `web/package.json` intentionally has zero UI widget dependencies beyond React.

**Decision.** Style `.input`, `.select`, and `.textarea` directly with `--craft-field`, `--craft-field-border`, `--craft-radius-sm`, `--craft-font-sans`, and `--craft-accent` focus rings, and honour `[data-craft-bevel="true"]` with Classic sunken text/number fields and raised select controls, while using `.craft-toggle.is-adaptive` and `.craft-check-row` for boolean toggles/checkboxes and `.craft-menu` / `.craft-menu-item` for custom action menus.

**Consequences.** Native keyboard navigation, `<optgroup>` grouping, date/time pickers, and automated UI checks continue to work identically while visually matching Pro, Studio, and Classic grammars.

## 4. Coordinate-space SVG overlays vs kit UI icons

**Context.** All chrome, navigation, and toolbar buttons need crisp `16×16` Craft DS icons (`[data-craft-icon]`), while `ScreenView` renders live click/drag/scroll overlays in remote display coordinates and `FlowCanvas` renders cubic bezier wires between node ports.

**Decision.** Replace all inline UI `<svg>` icons with `[data-craft-icon]` spans hydrated from `window.CRAFT_ICONS` via `web/src/lib/appearance.ts`, and retain `<svg>` exclusively for the two live data-visualisation layers (`ScreenView` action markers and `FlowCanvas` cubic wires), coloured via `--cu-marker`, `--cu-port-text`, and `--cu-port-screen`.

**Consequences.** Every UI icon comes from the 161-icon kit and passes `icon-names` and `named-controls` probes; data-driven vector overlays stay responsive and theme-aware.

## 5. Fixed node-card port geometry on the zoomed FlowCanvas

**Context.** `web/src/flow/graph.ts` computes port coordinates from fixed constants (`NODE_W = 224`, `HEADER_H = 40`, `PORT_TOP = 10`, `PORT_ROW = 24`) rather than reading `getBoundingClientRect()` on every pointer move, keeping panning and wiring deterministic at any canvas zoom.

**Decision.** Keep `.flow-node` header and port-row pixel heights synchronized with `web/src/flow/graph.ts` across all `--craft-font-scale` settings while styling node cards, headers, ports, and selection outlines with `--craft-card`, `--craft-tab-strip`, `--craft-separator`, `--craft-accent`, and `[data-craft-bevel="true"]`.

**Consequences.** Wires always terminate on exact port centres even under `large` UI font scaling (`1.333×`), while the surrounding toolbar, palette, and inspector scale with `--craft-font-scale`.
