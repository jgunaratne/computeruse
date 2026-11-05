# computeruse: agents start here

This file is read by every agent tool (`GEMINI.md` and `CLAUDE.md` import it via `@AGENTS.md`).

## Commands
- `make check` — Python lint (`ruff check .`), TypeScript check (`cd web && npx tsc --noEmit`), and pytest (`uv run pytest -q`).
- `make web` — Build the Vite frontend into web/dist (gitignored build output).
- `make ui-check` — Build web/dist, start a throwaway server, exercise every console page and flow editor interaction in headless Chrome, verify narrow-viewport layout (`820×900`), and write screenshots to `.ui-check/`.
- `~/git/craft-ds/bin/craft-ds check .` — Verify the Craft Design System harness, vendored kit hashes, rules, skills, hooks, and documentation links.
- `~/git/craft-ds/bin/craft-ds probe .` — Run the 15 Craft DS drift probes across `web/index.html`, `web/src/**/*.tsx`, `web/src/**/*.ts`, and `web/src/**/*.css`.

## Craft Design System
- This project's look is the Craft Design System (`web/public/vendor/craft-ds`, v0.16.0, vendored read-only from `~/git/craft-ds`). Use its tokens (`var(--craft-*)`), classes (`.craft-*`), icons (`data-craft-icon`) and themes (`data-craft-theme`); never hand-edit the kit (upgrade with `craft-ds update .`).
- Themes are tokens, grammars are flags: no colour, radius or font literals, no `[data-craft-theme=…]` patches, no `.craft-*` restyling. Deviations go under "Deliberate deviations" in `docs/DESIGN.md` and as a numbered entry in `docs/DECISIONS.md`.
- Read `docs/DESIGN.md` and the `craft-ds` rule before touching `web/index.html`, `web/src/**/*.tsx`, `web/src/**/*.ts`, or `web/src/**/*.css`.
- Before saying a UI change is done: `~/git/craft-ds/bin/craft-ds check .` and `~/git/craft-ds/bin/craft-ds probe .` are clean (or explained), `make check` and `make ui-check` pass, screenshots in `proMedium` (Pro (Medium Gray), pro grammar), `pro` (Pro (Dark), pro grammar), `proLight` (Pro (Light), pro grammar), `studio` (Studio (Dark), studio grammar), `studioLight` (Studio (Light), studio grammar), `classic` (Classic, classic grammar) at 1440, 820 px wide were looked at, and the `craft-design-reviewer` agent ran.

## Console UI invariants
- Keep `web/package.json` dependency-free beyond `react` and `react-dom` (one project stylesheet `web/src/styles.css` layered on `web/public/vendor/craft-ds/css/craft-ds.css`).
- Project CSS animations touch only `transform` and `opacity`; gate project `:hover` rules behind `@media (hover: hover) and (pointer: fine)` and keep `@media (prefers-reduced-motion: reduce)` in step.
- Keep `web/src/flow/graph.ts` node-card geometry constants (`NODE_W`, `NOTE_W`, `HEADER_H`, `PORT_TOP`, `PORT_ROW`) synchronized with `.flow-node*` in `web/src/styles.css` so SVG wires land cleanly on port dots.
- Commit only when asked; never push without explicit instruction.
