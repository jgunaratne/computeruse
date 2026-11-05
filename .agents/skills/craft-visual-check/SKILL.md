---
name: craft-visual-check
description: "Screenshot a computeruse page or component in every Craft DS grammar (Pro, Studio, Classic bevel) and UI font size, read every PNG against the Craft DS checklist, fix, and reshoot. Use after any visual change, before saying it is done, and whenever a theme switch "should be fine"."
---

# Visual check on Craft DS

Pictures, not assumptions. Themes change geometry here (pill vs rounded buttons, checkbox vs switch, bevels, colon labels, chip shapes), so one theme never proves the others.

## 1. Shoot

```sh
uv run python scripts/craft_shoot.py --url {url} --out {out} --width {width} --height {height} --theme {theme}
```
Defaults from `craft-ds.json`: themes `proMedium` (Pro (Medium Gray), pro grammar), `pro` (Pro (Dark), pro grammar), `proLight` (Pro (Light), pro grammar), `studio` (Studio (Dark), studio grammar), `studioLight` (Studio (Light), studio grammar), `classic` (Classic, classic grammar); widths 1440, 820 px; output `.ui-check/craft-shots/`. Add `--font-sizes small,large` when the change has text, `--full-page` for long pages. A target is a URL (dev server) or an HTML file; `?theme=` and `?font=` are set per shot and honoured by `craft-ds.js`, and `data-craft-theme` is pinned on `<html>` before load for pages without it.

No `node` with playwright/puppeteer here? Set `screenshots.command` in `craft-ds.json` (or `$CRAFT_SHOOT`) to your screenshot tool with `{url} {out} {width} {height}` placeholders; the CLI loops themes × widths for you.

## 2. Read every PNG

Open each image (the `report.json` beside them lists page errors, horizontal overflow, axe violations and whether the theme actually applied). Check:

- **Theme applied**: the page is in the theme the file name says (`report.json` → `applied.theme`), not stuck on the default.
- **Pro** (`proMedium`, `pro`, …): 28 px pill buttons, secondary buttons are outlined, `toggle.is-adaptive` renders as a 14 px checkbox, labels end with a colon, dark `#262626`/`#4a4a4a`-style tab strip above cards, 3-line panel menu icon, square overlapping colour chips.
- **Studio** (`studio`, `studioLight`, `adwaita*`, `breeze*`, `soft*`, `solarizedDark`): rounded cards, pill tabs, 30 px rounded buttons with a near-white/near-black primary, 30×17 px switches, no colons, ellipsis menu icon, round stacked chips.
- **Classic**: warm grey, square corners, raised bevel on buttons and bars, sunken bevel on fields, rectangular slider thumb.
- **Every theme**: text legible (4.5:1 body, 3:1 UI), nothing clipped or overlapping at the narrow width, long labels ellipsize (`.craft-elided`), icons are kit icons at 14–18 px with 1.75 stroke, focus visible on the first tabbable control.
- **Font sizes**: at `large` nothing wraps into a second line inside a 24 px field or a 28/30 px button; at `tiny` nothing becomes unreadable.
- **Fidelity**: the piece looks like its counterpart in the kit showcase (`web/public/vendor/craft-ds/index.html`) and PhotoCraft; differences are in "Deliberate deviations" in `docs/DESIGN.md`.
- `report.json`: zero page errors, zero overflow, no serious/critical axe violations.

## 3. Fix, reshoot, until right

Then `~/git/craft-ds/bin/craft-ds probe .` so the mechanical probes agree with your eyes. Don't leave a dev server running.

## Report template

```
Visual check: <urls or files>
- shots: <n> PNGs (<themes> × <widths>[ × <font sizes>]), read: yes
- findings: <none | file → what's wrong → fixed?>
- report.json: <errors / overflow / axe / theme applied>
- not checked: <hover, drag, real display scaling, …>
```
