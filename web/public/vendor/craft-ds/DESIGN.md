# Craft Design System (`craft-ds`) — Specification (`DESIGN.md`)

> Extracted from **PhotoCraft** (`crates/ui-egui/src/theme.rs`, `widgets.rs`, `panels.rs`, `icons.rs`, and PhotoCraft’s `ui-design.md`).

---

## 1. Visual Direction & Philosophy

**Craft DS** is a dense, precision desktop creative-tool design system built around three foundational principles:

1. **Neutral Chrome, Vibrant Canvas**: Interface chrome (`--craft-chrome`, `--craft-card`, `--craft-dock`) stays strictly achromatic or low-saturation so user artwork on the canvas (`--craft-canvas`) is never distorted by simultaneous color contrast.
2. **Three Structural Grammars in One Token System**: Rather than treating a theme as a simple color swap, Craft DS couples 31 semantic color/radius tokens with three structural layout flags (`pro`, `bevel`, `round_chips`) that adapt widget geometry, button silhouettes, panel headers, toggle idioms, lock bars, and color chips:
   - **Pro Spectrum Grammar (`pro: true`)**: Adobe Spectrum-inspired flat charcoal/gray panels (`#323232` Dark, `#535353` Medium Gray default, `#b8b8b8` Light Gray, `#f0f0f0` Light), `#378ef0` Spectrum blue accent, `3/4/6px` radii, `12px` body text, `28px` pill buttons (secondary buttons use a `1.5px` inside stroke over a transparent fill), `14×14px` checkboxes in place of pill toggles, 5-icon layer lock strips, colon-suffixed field labels (`Opacity:`), full-bleed blue hover in popup menus, monochrome gray text selection (`#606060` / `#c9c9c9`), and `18×18px` overlapping square color chips.
   - **Studio Card/Pill Grammar (`pro: false, bevel: false, round_chips: true`)**: Contemporary dark/light creative studio aesthetic (`#141415` carbon chrome, `#1a1a1c` cards, `#8b7cf6` violet accent, near-white `#f6f6f8` primary CTA buttons), `6/8/12px` radii, `12.5px` body text, `24px` pill tabs, `30×17px` iOS-style pill toggle switches, single lock icon button, colon-free labels (`Opacity`), and `30px` stacked round color chips with a curved swap arc. Also powers Linux/desktop themes (`GNOME Adwaita`, `KDE Breeze`, `Solarized Dark`, `Soft Dark/Light`).
   - **Classic Windows 2000 3D Bevel Grammar (`bevel: true`)**: Warm `#d4d0c8` surfaces, `0px` sharp corners, `#0a246a` navy selection, raised 2-pixel 3D bevels (`#ffffff` top-left, `#404040` bottom-right) on buttons/bars, sunken bevels on inputs, and `10×16px` rectangular 3D slider thumbs.
3. **High-Density Numeric & Direct-Manipulation Ergonomics**: Every numeric value field pairs a right-aligned monospace readout (`JetBrains Mono`) and dimmed unit suffix (`px`, `%`, `°`) with a horizontal drag-to-scrub label (`cursor: ew-resize`), mouse-wheel stepping (`Shift` = 10×, `Alt` = 0.1×), optional dropdown slider popover (`160px`), and live arithmetic evaluation (`1920/2`, `(30+15)*2`, `2^8`, `pi`).

---

## 2. Theme Catalog (`ThemeKind::ALL` — 14 Themes)

| ID (`data-craft-theme`) | `ThemeKind` | Grammar | `chrome` | `canvas` | `dock` | `card` | `field` | `text` | `accent` | `primary_bg` | Radii (`sm/md/lg`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `pro` | `Pro` | Pro (`pro: true`) | `#323232` | `#282828` | `#1e1e1e` | `#323232` | `#242424` | `#dedede` | `#378ef0` | `#378ef0` | `3 / 4 / 6 px` |
| `proMedium` *(Default)* | `ProMedium` | Pro (`pro: true`) | `#535353` | `#282828` | `#424242` | `#535353` | `#454545` | `#ededed` | `#378ef0` | `#378ef0` | `3 / 4 / 6 px` |
| `proLightGray` | `ProLightGray` | Pro (`pro: true`) | `#b8b8b8` | `#a0a0a0` | `#a8a8a8` | `#b8b8b8` | `#d4d4d4` | `#212121` | `#1473e6` | `#1473e6` | `3 / 4 / 6 px` |
| `proLight` | `ProLight` | Pro (`pro: true`) | `#f0f0f0` | `#d6d6d6` | `#e2e2e2` | `#f0f0f0` | `#ffffff` | `#1c1c1c` | `#1473e6` | `#1473e6` | `3 / 4 / 6 px` |
| `studio` | `Studio` | Studio (`round_chips`) | `#141415` | `#0e0e0f` | `#111112` | `#1a1a1c` | `#222225` | `#f2f2f4` | `#8b7cf6` | `#f6f6f8` | `6 / 8 / 12 px` |
| `studioLight` | `StudioLight` | Studio (`round_chips`) | `#f6f6f8` | `#e9eaee` | `#f0f1f4` | `#fcfcfd` | `#efeff2` | `#141418` | `#6c5ce7` | `#141418` | `6 / 8 / 12 px` |
| `classic` | `Classic` | Classic (`bevel: true`) | `#d4d0c8` | `#808080` | `#c8c4bc` | `#d4d0c8` | `#ffffff` | `#000000` | `#0a246a` | `#0a246a` | `0 / 0 / 0 px` |
| `adwaita` | `Adwaita` | Studio (`round_chips`) | `#ebebeb` | `#deddda` | `#f3f3f3` | `#fafafa` | `#ffffff` | `#2e2e32` | `#3584e4` | `#3584e4` | `6 / 9 / 12 px` |
| `adwaitaDark` | `AdwaitaDark` | Studio (`round_chips`) | `#242424` | `#1e1e1e` | `#202020` | `#303030` | `#383838` | `#ffffff` | `#3584e4` | `#3584e4` | `6 / 9 / 12 px` |
| `breezeLight` | `BreezeLight` | Studio (`round_chips`) | `#dee0e2` | `#eff0f1` | `#e8eaec` | `#fcfcfc` | `#ffffff` | `#232629` | `#3aace8` | `#3aace8` | `5 / 5 / 6 px` |
| `breezeDark` | `BreezeDark` | Studio (`round_chips`) | `#2a2e32` | `#1b1e20` | `#202326` | `#25292c` | `#1b1e20` | `#fcfcfc` | `#3aace8` | `#3aace8` | `5 / 5 / 6 px` |
| `solarizedDark` | `SolarizedDark` | Studio (`round_chips`) | `#073642` | `#002b36` | `#002b36` | `#073642` | `#00212b` | `#839496` | `#268bd2` | `#268bd2` | `6 / 8 / 12 px` |
| `softDark` | `SoftDark` | Studio (`round_chips`) | `#18171e` | `#121117` | `#15141b` | `#201f28` | `#282732` | `#dcdae6` | `#8a9ae8` | `#8a9ae8` | `7 / 10 / 14 px` |
| `softLight` | `SoftLight` | Studio (`round_chips`) | `#f5f4f8` | `#eae8f0` | `#efedf4` | `#fbfafd` | `#edeaf3` | `#2e2c38` | `#6e80d8` | `#6e80d8` | `7 / 10 / 14 px` |

---

## 3. Semantic Layer Label Palette (`Theme::layer_label_colors`)

Layers, groups, and artboards can be tagged with 11 semantic label colors. Each label defines a pair of colors: a vivid swatch for context menus (`swatch`) and a darkened, desaturated fill (`eye`) for the `28px` eye-visibility column with a light `#e2e2e2` eye icon:

| Label | Menu Swatch (`--craft-label-*`) | Eye Column Fill (`--craft-label-*-eye`) | Eye Icon |
| :--- | :--- | :--- | :--- |
| **Red** | `#fc5d5b` | `#9f2f30` | `#e2e2e2` |
| **Orange** | `#f79744` | `#934f0c` | `#e2e2e2` |
| **Yellow** | `#dcd64b` | `#99780c` | `#e2e2e2` |
| **Green** | `#85dc6a` | `#4e712e` | `#e2e2e2` |
| **Seafoam** | `#1c8488` | `#0b544f` | `#e2e2e2` |
| **Blue** | `#78adf6` | `#415b87` | `#e2e2e2` |
| **Indigo** | `#544be7` | `#36348e` | `#e2e2e2` |
| **Magenta** | `#cc1a7e` | `#981b51` | `#e2e2e2` |
| **Fuchsia** | `#b014c0` | `#710f74` | `#e2e2e2` |
| **Violet** | `#9176d5` | `#5d3f8e` | `#e2e2e2` |
| **Gray** | `#9c9c9c` | `#575757` | `#e2e2e2` |

---

## 4. Typography & UI Scaling

- **Bundled Font Families**:
  - `Inter` (`400` Regular, `500` Medium, `600` SemiBold) — UI chrome, tabs, buttons, menus, layer rows.
  - `JetBrains Mono` (`400` Regular) — Numeric value fields, unit suffixes (`px`, `%`, `°`), hex codes, ruler ticks, keyboard shortcuts.
- **Type Scale (`sizePt × --craft-font-scale`)**:
  - `Heading`: `Inter-SemiBold`, `15.0pt` (`--craft-fs-heading`)
  - `Button` / `TitleBar`: `Inter-Medium`, `13.0pt` (`--craft-fs-button`, `--craft-fs-titlebar`)
  - `PillTab`: `Inter-Medium`, `12.5pt` (`--craft-fs-pill-tab`)
  - `Body`: `Inter-Regular`, `12.0pt` in Pro (`pro: true`) / `12.5pt` in Studio (`--craft-fs-body`)
  - `Monospace`: `JetBrains Mono`, `12.0pt` (`--craft-fs-mono`)
  - `SectionHeader`: `Inter-Medium`, `11.5pt` (`--craft-fs-section`)
  - `UnitSuffix`: `JetBrains Mono`, `11.0pt` (`--craft-fs-unit`)
  - `Small`: `Inter-Regular`, `10.5pt` (`--craft-fs-small`)
- **UI Font Multiplier (`UiFontSize`)**:
  - `tiny`: `10 / 12` (`0.833333`)
  - `small` *(default)*: `1.0`
  - `medium`: `14 / 12` (`1.166667`)
  - `large`: `16 / 12` (`1.333333`)

---

## 5. Component & Chrome Metrics

| Component | Pro Metric (`pro: true`) | Studio Metric (`pro: false`) | Classic Override (`bevel: true`) |
| :--- | :--- | :--- | :--- |
| **Title Bar Height** | `32 px` | `38 px` | `38 px` + raised 3D bevel |
| **Options Bar Height** | `36 px` (`30 px` narrow) | `42 px` (`34 px` narrow) | `42 px` + raised 3D bevel |
| **Status Bar Height** | `24 px` (`18 px` controls) | `30 px` (`22 px` controls) | `30 px` + raised 3D bevel |
| **Toolbar Single Width** | `40 px` (`30×30 px` buttons) | `50 px` (`36×36 px` buttons) | `50 px` + raised 3D bevel |
| **Collapsed Dock Rail** | `36 px` (`28×28 px` buttons) | `44 px` (`32×32 px` buttons + `3×16px` left bar) | `44 px` |
| **Right Dock Width** | `290 px` (`250 px` min) | `300 px` (`250 px` min) | `300 px` |
| **Primary / Secondary Button** | `28 px` height, `14 px` pill radius | `30 px` height, `radius_sm` | `30 px` height, `0 px` radius + 3D bevel |
| **Compact Button** | `22 px` height | `22 px` height | `22 px` height + 3D bevel |
| **Value / Text Field** | `24 px` height (`14 px` popup arrow) | `24 px` height (`14 px` popup arrow) | `24 px` height + sunken 3D bevel |
| **Toggle Control** | Delegates to `14×14 px` checkbox | `30×17 px` pill switch (`13 px` thumb) | `14×14 px` sunken checkbox |
| **Slider Track & Knob** | `3 px` track (`5 px` grad), `12 px` knob (`#282828` outline) | `3 px` track (`5 px` grad), `14 px` knob | `10×16 px` rectangular 3D beveled thumb |
| **FG/BG Color Chips** | `18×18 px` square (`10 px` offset) | `30 px` round (`19 px` vertical offset) | `21×21 px` square (`12 px` offset) |
| **Angle Dial** | `42×42 px` circle (`15°` Shift snap) | `42×42 px` circle (`15°` Shift snap) | `42×42 px` circle |
| **Window / Popup Shadow** | `0 10px 32px` / `0 6px 20px` | `0 10px 32px` / `0 6px 20px` | Sharp `1px` `#808080` border |

---

## 6. Deliberate deviations

Everything that differs on purpose between this web kit and PhotoCraft's egui implementation. A difference not in this table is drift: fix the kit or add a row with a decision.

| What differs | PhotoCraft (reference) | Craft DS (web) | Why | Decision |
|---|---|---|---|---|
| Units | `pt` sizes in egui (`12.0`, `12.5`, `15.0`) | the same numbers as CSS `px`, scaled by `--craft-font-scale` | egui points render 1:1 with CSS px at 1× scale; the UI font multiplier reproduces `UiFontSize` | [#2](docs/DECISIONS.md) |
| Theme switching | `ThemeKind` enum applied at frame time | `data-craft-theme` + three grammar flags + `data-craft-dark` on `<html>` (or any `.craft-scope`) | lets several themes coexist on one page (the grammar comparison) and keeps CSS selectors static | [#1](docs/DECISIONS.md) |
| Classic bevels | two 1 px painted strokes (`draw_bevel`) | `box-shadow: inset 1px 1px #fff, inset -1px -1px #404040` | one declaration, no extra elements, flips cleanly for sunken fields | [#3](docs/DECISIONS.md) |
| Popup value field | a floating egui `Area` | a `.craft-popup-slider-popover` absolutely positioned under the field | DOM popovers keep focus and wheel handling in the same stacking context | [#4](docs/DECISIONS.md) |
| Checkbox glyph | vector check path | CSS-drawn `::after` with two borders rotated 45° | avoids an extra SVG per checkbox; identical at 14 px | — |
| Icon fallback | missing icon is a compile error | `data-craft-icon` with an unknown name falls back to `sparkles` at runtime and is a `craft-ds probe` finding (`icon-names`) | a web page must still render; the probe makes the slip visible | [#5](docs/DECISIONS.md) |
| Swap-arrow icons | drawn with `painter.line_segment` | inline SVG paths in `craft-ds.js` (`getProSwapArrowSvg`, `getStudioSwapArcSvg`) | they depend on the grammar, so they are rendered by the runtime, not the icon set | — |
| Theme persistence | preferences file | `?theme=` / `?font=` query, else the attribute already on `<html>`, else `proMedium` | makes every state a URL (screenshots, bug reports) without storage | [#6](docs/DECISIONS.md) |

## 7. Enforcement

The kit ships with the tooling that keeps a project on it: `bin/craft-ds` (`check`, `probe`, `apply`, `shoot`, `update`), a rule, four skills and a read-only reviewer rendered identically for Claude Code (`.claude/`) and Gemini/Antigravity (`.agents/`), and a guard hook that keeps the vendored kit read-only. See [README.md](README.md) and [MAPPING.md](MAPPING.md).
