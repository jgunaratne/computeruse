---
name: craft-new-component
description: "Build a new UI piece in computeruse on the Craft Design System the right way - find the kit component or pattern first, compose kit classes and tokens in a project class, keep it right in every grammar (Pro, Studio, Classic bevel) and UI font size, name its controls, then screenshot, probe and review. Use when a screen needs a look that no existing kit component gives, or when adding any new visual piece."
---

# New component on Craft DS

Read the `craft-ds` rule and `docs/DESIGN.md` first. Then open the kit showcase (`web/public/vendor/craft-ds/index.html`, sections 6–7) and `web/public/vendor/craft-ds/css/craft-ds.css`: extending a kit component beats a near-duplicate.

## 1. Find the counterpart

| You need… | Use |
|---|---|
| a button, icon button, compact action | `.craft-btn.craft-btn-primary|secondary|danger`, `.craft-icon-btn`, `.craft-btn-compact` |
| a number with units, scrubbing, math | `.craft-value-field` / `.craft-popup-value-field` + `input[data-craft-numeric]` + `.craft-scrub-label[data-scrub-target]` |
| on/off | `.craft-toggle.is-adaptive` (checkbox in Pro, switch in Studio) or `.craft-checkbox` |
| a range | `.craft-slider` (+ `.is-gradient`, `.is-bipolar`, `data-exponent`) |
| a panel with tabs | `.craft-panel` > `.craft-panel-header` > `.craft-tab-strip` > `.craft-tab`, then `.craft-panel-body`, `.craft-panel-footer` |
| a menu, a dialog | `.craft-menu` / `.craft-menu-item`, `.craft-modal-backdrop` > `.craft-dialog` |
| app chrome | `.craft-titlebar`, `.craft-options-bar`, `.craft-toolbar` / `.craft-tool-btn`, `.craft-rail`, `.craft-status-bar`, `.craft-doc-tabs`, `.craft-canvas-area` |
| rows, swatches, dials, curves | `.craft-layer-row`, `.craft-swatch`, `.craft-angle-dial`, `.craft-curve-box` |

If PhotoCraft has the widget (see `web/public/vendor/craft-ds/DESIGN.md` §5 metrics table and `web/public/vendor/craft-ds/index.html`), port its geometry: heights, radii, label conventions.

## 2. The markup

- Kit classes for the parts that exist, **plus one project class** for the new whole (`class="craft-panel app-inspector"`). Never invent a `craft-*` class.
- Icons: `<span class="craft-icon" data-craft-icon="<kit name>"></span>`; icon-only buttons get `aria-label` or `title`.
- Labels: `.craft-scrub-label.craft-pro-colon` for field labels (the colon appears in Pro only); `.craft-section-header` for group labels; readouts in `.craft-mono`.
- Grammar-aware bits use the flags, not theme ids: `[data-craft-pro="true"] .app-inspector { … }` is fine; `[data-craft-theme="studio"]` is not.

## 3. The CSS

In the project stylesheet (`web/src/**/*.css`), under a comment naming the component:
- tokens only: `var(--craft-card)`, `var(--craft-radius)`, `var(--craft-fs-body)`, `var(--craft-separator)`; a neutral `rgba(0,0,0,.4)` is fine for a shadow;
- project tokens, if any, get their own prefix and derive from kit tokens (`--app-inspector-w: 290px; --app-brand: var(--craft-accent)`);
- no selector that starts with or contains `.craft-` (style your project class), no `--craft-*` redefinition, no `[data-craft-theme=…]`.

## 4. Behaviour

Numeric fields, sliders, dials, toggles, tabs, popovers and dialogs are wired by `web/public/vendor/craft-ds/js/craft-ds.js` from the markup (`data-craft-numeric`, `data-min/max/step/decimals`, `data-linked-slider`, `data-scrub-target`, `data-tab-target`, `data-open-modal`). Call `CraftDS.setTheme(id)` / `CraftDS.setFontSize(size)` for appearance; listen to `craft-change` / `craft-value-commit` events. Don't fork the kit script.

## 5. Check it

```sh
~/git/craft-ds/bin/craft-ds probe . --only raw-colours,raw-values,unknown-tokens,class-names,icon-names,component-restyle,named-controls
uv run python scripts/craft_shoot.py --url {url} --out {out} --width {width} --height {height} --theme {theme}
```
Read every PNG (`proMedium` (Pro (Medium Gray), pro grammar), `pro` (Pro (Dark), pro grammar), `proLight` (Pro (Light), pro grammar), `studio` (Studio (Dark), studio grammar), `studioLight` (Studio (Light), studio grammar), `classic` (Classic, classic grammar); 1440, 820 px; and `small`, `large` UI font sizes if the component has text): see the `craft-visual-check` checklist. Then ask `craft-design-reviewer`.

## Report template

```
New component: <name> (<file>)
- built from: <kit classes used>
- project CSS: <n rules in <stylesheet>; project tokens: …>
- grammars checked: Pro ✓ Studio ✓ Classic ✓  (font sizes: …)
- probes: <clean | findings and why>
- shots read: <n>; issues found/fixed: …
- deviations recorded: <none | docs/DESIGN.md row + docs/DECISIONS.md #n>
```
