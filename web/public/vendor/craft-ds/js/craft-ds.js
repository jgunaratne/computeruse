/* ==========================================================================
   Craft Design System (craft-ds) Interactive Runtime
   Extracted from PhotoCraft (crates/ui-egui/src/theme.rs, widgets.rs, panels.rs)
   ========================================================================== */

(function () {
  'use strict';

  const SPEC = window.CRAFT_DS_SPEC || { themes: [], layerLabelColors: [], toolSections: [] };
  const ICONS = window.CRAFT_ICONS || {};

  const state = {
    themeId: SPEC.defaultTheme || 'proMedium',
    fontSize: 'small',
    platformOrder: 'win-linux',
    twoColumnToolbar: false,
    narrowOptionsBar: false,
    activeTool: 'Brush',
    fgColor: '#378ef0',
    bgColor: '#ffffff',
    brushSize: 48,
    brushHardness: 82,
    brushOpacity: 100,
    brushFlow: 100,
    brushAngle: 32,
    curveChannel: 'rgb',
    curvePoints: [
      { x: 0.0, y: 0.0 },
      { x: 0.25, y: 0.19 },
      { x: 0.72, y: 0.83 },
      { x: 1.0, y: 1.0 }
    ],
    activeCurvePoint: 2,
    layers: [
      { id: 'type-badge', name: 'CRAFT // 2026', kind: 'type', visible: true, locked: false, labelColor: 'violet', opacity: 100, fill: 100, blend: 'Normal', hasMask: false, fx: true },
      { id: 'curves-adj', name: 'Curves — Tone Grade', kind: 'adjustment', visible: true, locked: false, labelColor: 'blue', opacity: 92, fill: 100, blend: 'Normal', hasMask: true, fx: false },
      { id: 'kitsune-glow', name: 'Kitsune Rim Light', kind: 'pixel', visible: true, locked: false, labelColor: 'orange', opacity: 88, fill: 85, blend: 'Screen', hasMask: false, fx: true },
      { id: 'kitsune-hero', name: 'Kitsune Subject', kind: 'pixel', visible: true, locked: false, labelColor: 'red', opacity: 100, fill: 100, blend: 'Normal', hasMask: true, fx: true },
      { id: 'bg-gradient', name: 'Background Studio', kind: 'pixel', visible: true, locked: true, labelColor: 'seafoam', opacity: 100, fill: 100, blend: 'Normal', hasMask: false, fx: false }
    ],
    selectedLayerId: 'kitsune-hero'
  };

  /* ------------------------------------------------------------------------
     1. SVG Icon & Custom Vector Helpers
     ------------------------------------------------------------------------ */
  function getIconSvg(name) {
    if (ICONS[name]) return ICONS[name];
    return ICONS['sparkles'] || '';
  }

  // Pro 13x13 orthogonal double-headed swap arrow (widgets/swap_arrow.rs)
  function getProSwapArrowSvg() {
    return `<svg viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 4 4 Q 9 4 9 9" />
      <polyline points="5.6,2.2 3.2,4.0 5.6,5.8" />
      <polyline points="7.2,7.4 9.0,9.8 10.8,7.4" />
    </svg>`;
  }

  // Pro 13x13 default B/W mini squares icon (widgets/color.rs)
  function getDefaultChipIconSvg() {
    return `<svg viewBox="0 0 13 13" fill="none">
      <rect x="4.5" y="4.5" width="7.5" height="7.5" fill="#ffffff" stroke="#686868" stroke-width="1"/>
      <rect x="1" y="1" width="7.5" height="7.5" fill="#000000" stroke="#ffffff" stroke-width="0.8"/>
    </svg>`;
  }

  // Studio curved swap arc below 30px round chips (widgets/color.rs)
  function getStudioSwapArcSvg() {
    return `<svg viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 4 4 Q 12 12 20 4" />
      <polyline points="3.5,7.5 3.8,3.6 7.6,4.2" />
      <polyline points="16.4,4.2 20.2,3.6 20.5,7.5" />
    </svg>`;
  }

  function hydrateIcons(root = document) {
    root.querySelectorAll('[data-craft-icon]').forEach((el) => {
      const iconName = el.getAttribute('data-craft-icon');
      const svg = getIconSvg(iconName);
      if (svg) el.innerHTML = svg;
    });
    root.querySelectorAll('[data-craft-pro-swap]').forEach((el) => {
      el.innerHTML = getProSwapArrowSvg();
    });
    root.querySelectorAll('[data-craft-default-chip-icon]').forEach((el) => {
      el.innerHTML = getDefaultChipIconSvg();
    });
    root.querySelectorAll('[data-craft-studio-swap]').forEach((el) => {
      el.innerHTML = getStudioSwapArcSvg();
    });
  }

  /* ------------------------------------------------------------------------
     2. Arithmetic Expression Evaluator (widgets/fields.rs: eval_arithmetic)
        Supports +, -, *, /, %, ^, **, parentheses, unary +/-, pi, tau
     ------------------------------------------------------------------------ */
  function evalArithmetic(input) {
    if (typeof input !== 'string') return null;
    const raw = input.trim();
    if (!raw) return null;

    // Strip trailing unit suffixes like px, %, °, pt, dB
    const cleaned = raw.replace(/\s*(px|%|°|deg|pt|em|rem|ms|s)\s*$/i, '').trim();
    if (!cleaned) return null;

    // Tokenize
    const tokens = [];
    let i = 0;
    while (i < cleaned.length) {
      const ch = cleaned[i];
      if (/\s/.test(ch)) {
        i++;
        continue;
      }
      if (/[0-9.]/.test(ch)) {
        let numStr = '';
        while (i < cleaned.length && /[0-9._]/.test(cleaned[i])) {
          if (cleaned[i] !== '_') numStr += cleaned[i];
          i++;
        }
        const val = Number(numStr);
        if (!Number.isFinite(val)) return null;
        tokens.push({ type: 'num', value: val });
        continue;
      }
      if (/[a-zA-Z]/.test(ch)) {
        let id = '';
        while (i < cleaned.length && /[a-zA-Z]/.test(cleaned[i])) {
          id += cleaned[i];
          i++;
        }
        const lower = id.toLowerCase();
        if (lower === 'pi') {
          tokens.push({ type: 'num', value: Math.PI });
        } else if (lower === 'tau') {
          tokens.push({ type: 'num', value: Math.PI * 2 });
        } else {
          return null;
        }
        continue;
      }
      if (ch === '*' && cleaned[i + 1] === '*') {
        tokens.push({ type: 'op', value: '^' });
        i += 2;
        continue;
      }
      if ('+-*/%^()'.includes(ch)) {
        tokens.push({ type: ch === '(' || ch === ')' ? ch : 'op', value: ch });
        i++;
        continue;
      }
      return null;
    }

    let pos = 0;
    function peek() {
      return tokens[pos] || null;
    }
    function consume() {
      return tokens[pos++] || null;
    }

    function parseExpr() {
      let left = parseTerm();
      if (left === null) return null;
      while (peek() && peek().type === 'op' && (peek().value === '+' || peek().value === '-')) {
        const op = consume().value;
        const right = parseTerm();
        if (right === null) return null;
        left = op === '+' ? left + right : left - right;
      }
      return left;
    }

    function parseTerm() {
      let left = parsePower();
      if (left === null) return null;
      while (peek() && peek().type === 'op' && (peek().value === '*' || peek().value === '/' || peek().value === '%')) {
        const op = consume().value;
        const right = parsePower();
        if (right === null) return null;
        if ((op === '/' || op === '%') && right === 0) return null;
        if (op === '*') left = left * right;
        else if (op === '/') left = left / right;
        else left = left % right;
      }
      return left;
    }

    function parsePower() {
      const base = parseUnary();
      if (base === null) return null;
      if (peek() && peek().type === 'op' && peek().value === '^') {
        consume();
        const exp = parsePower(); // right-associative
        if (exp === null) return null;
        return Math.pow(base, exp);
      }
      return base;
    }

    function parseUnary() {
      if (peek() && peek().type === 'op' && (peek().value === '+' || peek().value === '-')) {
        const op = consume().value;
        const inner = parseUnary();
        if (inner === null) return null;
        return op === '-' ? -inner : inner;
      }
      return parsePrimary();
    }

    function parsePrimary() {
      const tok = peek();
      if (!tok) return null;
      if (tok.type === 'num') {
        consume();
        return tok.value;
      }
      if (tok.type === '(') {
        consume();
        const val = parseExpr();
        if (val === null) return null;
        if (!peek() || peek().type !== ')') return null;
        consume();
        return val;
      }
      return null;
    }

    const result = parseExpr();
    if (result === null || pos !== tokens.length || !Number.isFinite(result)) {
      return null;
    }
    return result;
  }

  /* ------------------------------------------------------------------------
     3. Color Contrast & Formatting Utilities
     ------------------------------------------------------------------------ */
  function parseColorToRgba(str) {
    if (!str) return [0, 0, 0, 1];
    const s = str.trim();
    if (s.startsWith('#')) {
      const hex = s.slice(1);
      if (hex.length === 3) {
        return [
          parseInt(hex[0] + hex[0], 16),
          parseInt(hex[1] + hex[1], 16),
          parseInt(hex[2] + hex[2], 16),
          1
        ];
      }
      return [
        parseInt(hex.slice(0, 2), 16),
        parseInt(hex.slice(2, 4), 16),
        parseInt(hex.slice(4, 6), 16),
        1
      ];
    }
    const m = s.match(/rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)/i);
    if (m) {
      return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] !== undefined ? Number(m[4]) : 1];
    }
    return [0, 0, 0, 1];
  }

  function relLuminance([r, g, b]) {
    const chan = (c) => {
      const v = c / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b);
  }

  function contrastRatio(c1, c2) {
    const l1 = relLuminance(parseColorToRgba(c1));
    const l2 = relLuminance(parseColorToRgba(c2));
    const bright = Math.max(l1, l2);
    const dark = Math.min(l1, l2);
    return (bright + 0.05) / (dark + 0.05);
  }

  function showToast(msg) {
    const toast = document.getElementById('ds-toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add('is-visible');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      toast.classList.remove('is-visible');
    }, 1800);
  }

  /* ------------------------------------------------------------------------
     4. Theme & Appearance Engine
     ------------------------------------------------------------------------ */
  function getTheme(themeId) {
    return SPEC.themes.find((t) => t.id === themeId) || SPEC.themes[1] || SPEC.themes[0];
  }

  function setTheme(themeId) {
    const theme = getTheme(themeId);
    if (!theme) return;
    state.themeId = theme.id;

    const targets = [document.documentElement, document.body];
    targets.forEach((el) => {
      if (!el) return;
      el.setAttribute('data-craft-theme', theme.id);
      el.setAttribute('data-craft-pro', String(theme.pro));
      el.setAttribute('data-craft-bevel', String(theme.bevel));
      el.setAttribute('data-craft-round-chips', String(theme.round_chips));
      el.setAttribute('data-craft-dark', String(theme.dark));
    });

    // Update topbar selector & badges
    const select = document.getElementById('ds-theme-select');
    if (select && select.value !== theme.id) {
      select.value = theme.id;
    }

    const grammarBadge = document.getElementById('ds-active-grammar-badge');
    if (grammarBadge) {
      const grammar = theme.pro
        ? 'Pro Spectrum Grammar (pro: true)'
        : theme.bevel
        ? 'Classic Win2000 Bevel (bevel: true)'
        : 'Studio Card/Pill Grammar (round_chips: true)';
      grammarBadge.textContent = grammar;
    }

    // Highlight active theme card
    document.querySelectorAll('.ds-theme-card').forEach((card) => {
      card.classList.toggle('is-active', card.getAttribute('data-theme-id') === theme.id);
    });

    renderTokenMatrix(theme);
    drawWorkspaceCanvas();
    drawPointCurve();
    drawHistograms();
  }

  function cycleAppearance() {
    const idx = SPEC.themes.findIndex((t) => t.id === state.themeId);
    const next = SPEC.themes[(idx + 1) % SPEC.themes.length];
    if (next) {
      setTheme(next.id);
      showToast(`Theme: ${next.label}`);
    }
  }

  function setFontSize(sizeKey) {
    state.fontSize = sizeKey;
    document.documentElement.setAttribute('data-craft-font-size', sizeKey);
    document.body.setAttribute('data-craft-font-size', sizeKey);
    document.querySelectorAll('[data-font-size-btn]').forEach((btn) => {
      btn.classList.toggle('is-active', btn.getAttribute('data-font-size-btn') === sizeKey);
    });
  }

  /* ------------------------------------------------------------------------
     5. Theme Gallery & Live Semantic Token Matrix Rendering
     ------------------------------------------------------------------------ */
  function renderThemeGallery() {
    const grid = document.getElementById('ds-theme-gallery');
    if (!grid) return;

    grid.innerHTML = SPEC.themes
      .map((t) => {
        const grammarTag = t.pro ? 'Pro' : t.bevel ? 'Bevel' : 'Studio';
        const btnRadius = t.pro ? '9px' : t.bevel ? '0px' : `${t.radius_sm}px`;
        return `
        <div class="ds-theme-card ${t.id === state.themeId ? 'is-active' : ''}" data-theme-id="${t.id}" role="button" tabindex="0">
          <div class="ds-theme-mini-preview" style="background:${t.canvas}; border-color:${t.card_border};">
            <div style="grid-column: 1 / -1; background:${t.chrome}; border-bottom:1px solid ${t.separator}; display:flex; align-items:center; justify-content:space-between; padding:0 5px;">
              <span style="width:18px; height:4px; border-radius:2px; background:${t.text_dim};"></span>
              <span style="width:8px; height:8px; background:${t.caption_close};"></span>
            </div>
            <div style="background:${t.chrome}; border-right:1px solid ${t.separator}; display:flex; flex-direction:column; align-items:center; padding-top:4px; gap:3px;">
              <span style="width:8px; height:8px; border-radius:2px; background:${t.accent_soft}; border:1px solid ${t.accent_border};"></span>
              <span style="width:7px; height:7px; border-radius:${t.round_chips ? '50%' : '1px'}; background:${t.accent};"></span>
            </div>
            <div style="background:${t.canvas}; display:flex; align-items:center; justify-content:center; padding:4px;">
              <div style="width:74%; height:72%; background:${t.card}; border:1px solid ${t.card_border}; border-radius:${t.radius}px; display:flex; align-items:center; justify-content:center; gap:4px; padding:0 4px;">
                <span style="height:10px; padding:0 6px; border-radius:${btnRadius}; background:${t.primary_bg}; color:${t.primary_text}; font-size:7px; font-weight:600; display:inline-flex; align-items:center;">OK</span>
                <span style="width:10px; height:10px; border-radius:2px; background:${t.field}; border:1px solid ${t.field_border};"></span>
              </div>
            </div>
            <div style="background:${t.dock}; border-left:1px solid ${t.separator}; padding:3px; display:flex; flex-direction:column; gap:2px;">
              <div style="height:6px; background:${t.tab_strip};"></div>
              <div style="flex:1; background:${t.card}; border:1px solid ${t.card_border}; border-radius:${t.pro ? 0 : t.radius_sm}px; padding:2px;">
                <div style="height:4px; width:75%; background:${t.accent}; border-radius:1px;"></div>
              </div>
            </div>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; gap:4px;">
            <strong style="font-size:var(--craft-fs-body); color:var(--craft-text);">${t.label}</strong>
            <span class="ds-badge">${grammarTag}</span>
          </div>
          <div class="craft-small craft-elided" title="${t.description}">${t.description}</div>
          <div style="display:flex; align-items:center; gap:4px; margin-top:auto;">
            <span style="width:12px; height:12px; border-radius:2px; background:${t.chrome}; border:1px solid rgba(128,128,128,0.4);" title="chrome: ${t.chrome}"></span>
            <span style="width:12px; height:12px; border-radius:2px; background:${t.canvas}; border:1px solid rgba(128,128,128,0.4);" title="canvas: ${t.canvas}"></span>
            <span style="width:12px; height:12px; border-radius:2px; background:${t.card}; border:1px solid rgba(128,128,128,0.4);" title="card: ${t.card}"></span>
            <span style="width:12px; height:12px; border-radius:2px; background:${t.field}; border:1px solid rgba(128,128,128,0.4);" title="field: ${t.field}"></span>
            <span style="width:12px; height:12px; border-radius:2px; background:${t.accent}; border:1px solid rgba(128,128,128,0.4);" title="accent: ${t.accent}"></span>
            <span style="width:12px; height:12px; border-radius:2px; background:${t.primary_bg}; border:1px solid rgba(128,128,128,0.4);" title="primary_bg: ${t.primary_bg}"></span>
            <span class="craft-mono craft-faint" style="margin-left:auto; font-size:10px;">${t.kind}</span>
          </div>
        </div>`;
      })
      .join('');

    grid.querySelectorAll('.ds-theme-card').forEach((card) => {
      card.addEventListener('click', () => {
        const id = card.getAttribute('data-theme-id');
        setTheme(id);
        showToast(`Activated ${getTheme(id).label}`);
      });
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          card.click();
        }
      });
    });
  }

  const TOKEN_KEYS = [
    { key: 'chrome', css: '--craft-chrome', role: 'Title bar, options bar, toolbars, status bar' },
    { key: 'canvas', css: '--craft-canvas', role: 'Canvas pasteboard surround' },
    { key: 'dock', css: '--craft-dock', role: 'Right dock container background' },
    { key: 'card', css: '--craft-card', role: 'Panel surface, dialog & menu fill' },
    { key: 'card_border', css: '--craft-card-border', role: 'Panel card & dialog border stroke' },
    { key: 'tab_strip', css: '--craft-tab-strip', role: 'Inactive dock tab strip & doc tab bar' },
    { key: 'field', css: '--craft-field', role: 'Input fields, checkboxes, secondary buttons' },
    { key: 'field_border', css: '--craft-field-border', role: 'Input field & control outline' },
    { key: 'hover', css: '--craft-hover', role: 'Hovered button, row, or tab fill' },
    { key: 'pressed', css: '--craft-pressed', role: 'Pressed control state' },
    { key: 'row_selected', css: '--craft-row-selected', role: 'Selected layer/channel/path row fill' },
    { key: 'text', css: '--craft-text', role: 'Primary UI text & active labels' },
    { key: 'text_dim', css: '--craft-text-dim', role: 'Secondary labels, scrubby labels, units' },
    { key: 'text_faint', css: '--craft-text-faint', role: 'Placeholders, shortcuts, disabled text' },
    { key: 'icon', css: '--craft-icon', role: 'Default toolbar & panel icon color' },
    { key: 'accent', css: '--craft-accent', role: 'Focus ring, slider fill, active checkbox' },
    { key: 'accent_soft', css: '--craft-accent-soft', role: 'Selected tool / icon button background' },
    { key: 'accent_border', css: '--craft-accent-border', role: 'Selected tool / row border' },
    { key: 'accent_text', css: '--craft-accent-text', role: 'Active tool icon / pill text' },
    { key: 'primary_bg', css: '--craft-primary-bg', role: 'Primary CTA button background' },
    { key: 'primary_text', css: '--craft-primary-text', role: 'Primary CTA button label' },
    { key: 'separator', css: '--craft-separator', role: '1px structural dividers & hairlines' },
    { key: 'danger', css: '--craft-danger', role: 'Destructive action & error state' },
    { key: 'warning', css: '--craft-warning', role: 'Warning badge & alert indicator' },
    { key: 'caption_close', css: '--craft-caption-close', role: 'Title-bar close button hover fill' },
    { key: 'field_selection', css: '--craft-field-selection', role: 'Text input selection highlight' },
    { key: 'knob_outline', css: '--craft-knob-outline', role: '1px slider knob border outline' },
    { key: 'histogram_bg', css: '--craft-histogram-bg', role: 'Curves & histogram plot background' }
  ];

  function renderTokenMatrix(theme) {
    const matrix = document.getElementById('ds-token-matrix');
    if (!matrix) return;

    matrix.innerHTML = TOKEN_KEYS.map((item) => {
      const val = theme[item.key];
      return `
        <div class="ds-token-cell" data-copy-token="${item.css}: ${val};" title="Click to copy ${item.css}">
          <span class="ds-token-swatch" style="background:${val};"></span>
          <div style="min-width:0; flex:1;">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:4px;">
              <code class="craft-mono" style="font-size:11px; color:var(--craft-text); font-weight:500;">${item.key}</code>
              <code class="craft-mono" style="font-size:10.5px; color:var(--craft-accent);">${val}</code>
            </div>
            <div class="craft-small craft-elided" style="font-size:10px;">${item.role}</div>
          </div>
        </div>
      `;
    }).join('');

    matrix.querySelectorAll('[data-copy-token]').forEach((cell) => {
      cell.addEventListener('click', () => {
        const text = cell.getAttribute('data-copy-token');
        if (navigator.clipboard) navigator.clipboard.writeText(text);
        showToast(`Copied ${text}`);
      });
    });

    // Update live WCAG contrast audit bar
    const auditEl = document.getElementById('ds-contrast-audit');
    if (auditEl) {
      const pairs = [
        { label: 'text on card', fg: theme.text, bg: theme.card, min: 4.5 },
        { label: 'text on chrome', fg: theme.text, bg: theme.chrome, min: 4.5 },
        { label: 'text_dim on card', fg: theme.text_dim, bg: theme.card, min: 3.0 },
        { label: 'primary_text on primary_bg', fg: theme.primary_text, bg: theme.primary_bg, min: 4.5 }
      ];
      auditEl.innerHTML = pairs
        .map((p) => {
          const ratio = contrastRatio(p.fg, p.bg);
          const pass = ratio >= p.min;
          return `
            <span class="ds-badge" style="border-color:${pass ? 'var(--craft-field-border)' : 'var(--craft-warning)'};">
              <span style="width:8px; height:8px; border-radius:50%; background:${pass ? '#4cd964' : 'var(--craft-warning)'};"></span>
              <span>${p.label}: <strong>${ratio.toFixed(2)}:1</strong> (${pass ? 'PASS' : 'LOW'})</span>
            </span>
          `;
        })
        .join('');
    }
  }

  function renderLayerLabelSwatches() {
    const container = document.getElementById('ds-layer-label-palette');
    if (!container) return;
    container.innerHTML = SPEC.layerLabelColors
      .map(
        (c) => `
      <div class="ds-token-cell" data-copy-token="--craft-label-${c.id}: ${c.swatch};" title="Click to apply '${c.label}' to selected layer">
        <div style="display:flex; border-radius:3px; overflow:hidden; border:1px solid var(--craft-field-border); flex-shrink:0;">
          <span style="width:18px; height:26px; background:${c.swatch};" title="Menu swatch ${c.swatch}"></span>
          <span style="width:18px; height:26px; background:${c.eye}; color:${c.icon}; display:flex; align-items:center; justify-content:center;" title="Eye column fill ${c.eye}">
            <span class="craft-icon craft-icon-sm" data-craft-icon="eye" style="color:${c.icon};"></span>
          </span>
        </div>
        <div style="min-width:0; flex:1;">
          <div style="display:flex; align-items:center; justify-content:space-between;">
            <strong style="font-size:var(--craft-fs-body);">${c.label}</strong>
            <code class="craft-mono" style="font-size:10px; color:var(--craft-text-dim);">${c.swatch}</code>
          </div>
          <div class="craft-mono craft-small" style="font-size:10px;">eye: ${c.eye}</div>
        </div>
      </div>`
      )
      .join('');
    hydrateIcons(container);

    container.querySelectorAll('.ds-token-cell').forEach((cell, idx) => {
      cell.addEventListener('click', () => {
        const colorObj = SPEC.layerLabelColors[idx];
        const activeLayer = state.layers.find((l) => l.id === state.selectedLayerId);
        if (activeLayer && colorObj) {
          activeLayer.labelColor = colorObj.id;
          renderWorkspaceLayers();
          showToast(`Set layer '${activeLayer.name}' label to ${colorObj.label}`);
        }
      });
    });
  }

  /* ------------------------------------------------------------------------
     6. Interactive Sliders, Value Fields, Scrubby Labels & Popup Value Fields
     ------------------------------------------------------------------------ */
  function formatUnitValue(val, decimals = 0) {
    const factor = Math.pow(10, decimals);
    const rounded = Math.round(val * factor) / factor;
    return decimals > 0 ? rounded.toFixed(decimals) : String(rounded);
  }

  function valueToNorm(val, min, max, exponent = 1) {
    const clamped = Math.max(min, Math.min(max, val));
    if (max <= min) return 0;
    const linear = (clamped - min) / (max - min);
    return exponent !== 1 ? Math.pow(linear, 1 / exponent) : linear;
  }

  function normToValue(t, min, max, exponent = 1) {
    const clamped = Math.max(0, Math.min(1, t));
    const curved = exponent !== 1 ? Math.pow(clamped, exponent) : clamped;
    return min + curved * (max - min);
  }

  function bindValueControlGroup(root = document) {
    // Bind all .craft-slider elements
    root.querySelectorAll('.craft-slider').forEach((slider) => {
      if (slider._craftBound) return;
      slider._craftBound = true;

      const min = parseFloat(slider.getAttribute('data-min') ?? '0');
      const max = parseFloat(slider.getAttribute('data-max') ?? '100');
      const def = parseFloat(slider.getAttribute('data-default') ?? String(min));
      const decimals = parseInt(slider.getAttribute('data-decimals') ?? '0', 10);
      const exponent = parseFloat(slider.getAttribute('data-exponent') ?? '1');
      let current = parseFloat(slider.getAttribute('data-value') ?? String(def));

      const fill = slider.querySelector('.craft-slider-fill');
      const knob = slider.querySelector('.craft-slider-knob');
      const targetId = slider.getAttribute('data-target-input');

      function updateVisual(val, emit = true) {
        current = Math.max(min, Math.min(max, val));
        slider.setAttribute('data-value', String(current));
        const pct = (valueToNorm(current, min, max, exponent) * 100).toFixed(2) + '%';
        slider.style.setProperty('--craft-slider-pct', pct);
        if (fill && slider.classList.contains('is-bipolar')) {
          const zeroPct = valueToNorm(0, min, max, exponent) * 100;
          const curPct = valueToNorm(current, min, max, exponent) * 100;
          const left = Math.min(zeroPct, curPct);
          const width = Math.abs(curPct - zeroPct);
          fill.style.left = `${left}%`;
          fill.style.width = `${width}%`;
        }
        if (targetId) {
          const inp = document.getElementById(targetId);
          if (inp && document.activeElement !== inp) {
            inp.value = formatUnitValue(current, decimals);
          }
        }
        if (emit) {
          slider.dispatchEvent(new CustomEvent('craft-change', { detail: { value: current } }));
        }
      }

      slider._setValue = updateVisual;
      updateVisual(current, false);

      function setFromClientX(clientX) {
        const rect = slider.getBoundingClientRect();
        if (rect.width <= 0) return;
        const t = (clientX - rect.left) / rect.width;
        updateVisual(normToValue(t, min, max, exponent), true);
      }

      slider.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        slider.setPointerCapture(e.pointerId);
        slider.classList.add('is-dragging');
        setFromClientX(e.clientX);
      });
      slider.addEventListener('pointermove', (e) => {
        if (!slider.classList.contains('is-dragging')) return;
        setFromClientX(e.clientX);
      });
      const stopDrag = () => slider.classList.remove('is-dragging');
      slider.addEventListener('pointerup', stopDrag);
      slider.addEventListener('pointercancel', stopDrag);

      // Double-click resets to default (widgets/slider.rs)
      slider.addEventListener('dblclick', () => {
        updateVisual(def, true);
        showToast(`Reset slider to default (${formatUnitValue(def, decimals)})`);
      });
    });

    // Bind numeric inputs inside .craft-value-field and .craft-popup-value-field
    root.querySelectorAll('input[data-craft-numeric]').forEach((inp) => {
      if (inp._craftBound) return;
      inp._craftBound = true;

      const min = parseFloat(inp.getAttribute('data-min') ?? '0');
      const max = parseFloat(inp.getAttribute('data-max') ?? '100');
      const step = parseFloat(inp.getAttribute('data-step') ?? '1');
      const decimals = parseInt(inp.getAttribute('data-decimals') ?? '0', 10);
      const linkedSliderId = inp.getAttribute('data-linked-slider');

      function commitValue(rawVal) {
        const parsed = typeof rawVal === 'number' ? rawVal : evalArithmetic(String(rawVal));
        if (parsed === null) {
          // Revert to current valid value
          const prev = parseFloat(inp.getAttribute('data-current') ?? inp.value) || 0;
          inp.value = formatUnitValue(prev, decimals);
          return;
        }
        const clamped = Math.max(min, Math.min(max, parsed));
        inp.setAttribute('data-current', String(clamped));
        inp.value = formatUnitValue(clamped, decimals);
        if (linkedSliderId) {
          const sl = document.getElementById(linkedSliderId);
          if (sl && sl._setValue) sl._setValue(clamped, true);
        }
        inp.dispatchEvent(new CustomEvent('craft-value-commit', { detail: { value: clamped }, bubbles: true }));
      }

      inp._commitValue = commitValue;
      inp.setAttribute('data-current', inp.value);

      inp.addEventListener('focus', () => inp.select());
      inp.addEventListener('blur', () => commitValue(inp.value));
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          commitValue(inp.value);
          inp.blur();
        } else if (e.key === 'Escape') {
          const prev = parseFloat(inp.getAttribute('data-current') ?? '0');
          inp.value = formatUnitValue(prev, decimals);
          inp.blur();
        } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
          e.preventDefault();
          const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
          const dir = e.key === 'ArrowUp' ? 1 : -1;
          const cur = evalArithmetic(inp.value) ?? parseFloat(inp.getAttribute('data-current') ?? '0');
          commitValue(cur + dir * step * mult);
        }
      });

      // Mouse wheel stepping
      inp.parentElement.addEventListener(
        'wheel',
        (e) => {
          if (document.activeElement !== inp && !inp.parentElement.matches(':hover')) return;
          e.preventDefault();
          const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
          const dir = e.deltaY < 0 ? 1 : -1;
          const cur = evalArithmetic(inp.value) ?? parseFloat(inp.getAttribute('data-current') ?? '0');
          commitValue(cur + dir * step * mult);
        },
        { passive: false }
      );
    });

    // Bind Scrubby Labels ([data-scrub-target])
    root.querySelectorAll('.craft-scrub-label[data-scrub-target]').forEach((label) => {
      if (label._craftBound) return;
      label._craftBound = true;

      let startX = 0;
      let startVal = 0;
      const targetId = label.getAttribute('data-scrub-target');

      label.addEventListener('pointerdown', (e) => {
        const inp = document.getElementById(targetId);
        if (!inp) return;
        label.setPointerCapture(e.pointerId);
        label.classList.add('is-scrubbing');
        startX = e.clientX;
        startVal = parseFloat(inp.getAttribute('data-current') ?? inp.value) || 0;
      });

      label.addEventListener('pointermove', (e) => {
        if (!label.classList.contains('is-scrubbing')) return;
        const inp = document.getElementById(targetId);
        if (!inp || !inp._commitValue) return;
        const step = parseFloat(inp.getAttribute('data-step') ?? '1');
        const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
        const dx = e.clientX - startX;
        inp._commitValue(startVal + Math.round(dx / 2) * step * mult);
      });

      const endScrub = () => label.classList.remove('is-scrubbing');
      label.addEventListener('pointerup', endScrub);
      label.addEventListener('pointercancel', endScrub);
    });

    // Bind Popup Value Field Arrow Buttons
    root.querySelectorAll('.craft-popup-value-field').forEach((field) => {
      if (field._craftBound) return;
      field._craftBound = true;
      const arrow = field.querySelector('.craft-popup-value-arrow');
      if (arrow) {
        arrow.addEventListener('click', (e) => {
          e.stopPropagation();
          const wasOpen = field.classList.contains('is-open');
          document.querySelectorAll('.craft-popup-value-field.is-open').forEach((f) => f.classList.remove('is-open'));
          if (!wasOpen) field.classList.add('is-open');
        });
      }
    });
  }

  /* ------------------------------------------------------------------------
     7. Interactive 42x42 Angle Dial (widgets/angle_dial.rs)
     ------------------------------------------------------------------------ */
  function initAngleDials() {
    document.querySelectorAll('.craft-angle-dial').forEach((dial) => {
      const targetInputId = dial.getAttribute('data-angle-input');
      let angleDeg = parseFloat(dial.getAttribute('data-angle') ?? '30');

      function renderDial(deg, emit = true) {
        angleDeg = Math.max(-180, Math.min(180, Math.round(deg)));
        dial.setAttribute('data-angle', String(angleDeg));
        const rad = (angleDeg * Math.PI) / 180;
        const cx = 21;
        const cy = 21;
        const r = 16;
        // PhotoCraft angle dial convention: 0 deg = right (+x), counter-clockwise positive in math or standard screen
        const ex = cx + r * Math.cos(-rad);
        const ey = cy + r * Math.sin(-rad);

        dial.innerHTML = `
          <svg viewBox="0 0 42 42">
            <circle cx="21" cy="21" r="2.2" fill="var(--craft-text-dim)" />
            <line x1="21" y1="21" x2="${ex.toFixed(2)}" y2="${ey.toFixed(2)}" stroke="var(--craft-text)" stroke-width="1.5" stroke-linecap="round" />
            <circle cx="${ex.toFixed(2)}" cy="${ey.toFixed(2)}" r="3.2" fill="var(--craft-accent)" />
          </svg>
        `;
        if (targetInputId) {
          const inp = document.getElementById(targetInputId);
          if (inp && document.activeElement !== inp) {
            inp.value = String(angleDeg);
            inp.setAttribute('data-current', String(angleDeg));
          }
        }
        if (emit && dial.id === 'ws-brush-angle-dial') {
          state.brushAngle = angleDeg;
          drawWorkspaceCanvas();
        }
      }

      dial._setAngle = renderDial;
      renderDial(angleDeg, false);

      function updateFromPointer(e) {
        const rect = dial.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const dx = e.clientX - cx;
        const dy = -(e.clientY - cy);
        let deg = (Math.atan2(dy, dx) * 180) / Math.PI;
        if (e.shiftKey) {
          deg = Math.round(deg / 15) * 15;
        }
        renderDial(deg, true);
      }

      dial.addEventListener('pointerdown', (e) => {
        dial.setPointerCapture(e.pointerId);
        dial.classList.add('is-dragging');
        updateFromPointer(e);
      });
      dial.addEventListener('pointermove', (e) => {
        if (!dial.classList.contains('is-dragging')) return;
        updateFromPointer(e);
      });
      const stop = () => dial.classList.remove('is-dragging');
      dial.addEventListener('pointerup', stop);
      dial.addEventListener('pointercancel', stop);

      if (targetInputId) {
        const inp = document.getElementById(targetInputId);
        if (inp) {
          inp.addEventListener('craft-value-commit', (e) => {
            renderDial(e.detail.value, true);
          });
        }
      }
    });
  }

  /* ------------------------------------------------------------------------
     8. Monotone Cubic Point Curve Editor & RGB Histogram (widgets/point_curve.rs)
     ------------------------------------------------------------------------ */
  function evalMonotoneCubic(points, x) {
    const pts = [...points].sort((a, b) => a.x - b.x);
    const n = pts.length;
    if (n === 0) return x;
    if (n === 1) return pts[0].y;
    if (x <= pts[0].x) return pts[0].y;
    if (x >= pts[n - 1].x) return pts[n - 1].y;

    const dx = [];
    const dy = [];
    const m = [];
    for (let i = 0; i < n - 1; i++) {
      dx[i] = Math.max(1e-5, pts[i + 1].x - pts[i].x);
      dy[i] = pts[i + 1].y - pts[i].y;
      m[i] = dy[i] / dx[i];
    }

    const tangents = new Array(n).fill(0);
    tangents[0] = m[0];
    tangents[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) {
      if (m[i - 1] * m[i] <= 0) {
        tangents[i] = 0;
      } else {
        tangents[i] = (m[i - 1] + m[i]) / 2;
      }
    }

    for (let i = 0; i < n - 1; i++) {
      if (Math.abs(m[i]) < 1e-6) {
        tangents[i] = 0;
        tangents[i + 1] = 0;
      } else {
        const a = tangents[i] / m[i];
        const b = tangents[i + 1] / m[i];
        const s = a * a + b * b;
        if (s > 9) {
          const tau = 3 / Math.sqrt(s);
          tangents[i] = tau * a * m[i];
          tangents[i + 1] = tau * b * m[i];
        }
      }
    }

    let seg = 0;
    for (let i = 0; i < n - 1; i++) {
      if (x >= pts[i].x && x <= pts[i + 1].x) {
        seg = i;
        break;
      }
    }

    const h = dx[seg];
    const t = (x - pts[seg].x) / h;
    const t2 = t * t;
    const t3 = t2 * t;
    const h00 = 2 * t3 - 3 * t2 + 1;
    const h10 = t3 - 2 * t2 + t;
    const h01 = -2 * t3 + 3 * t2;
    const h11 = t3 - t2;
    const y = h00 * pts[seg].y + h10 * h * tangents[seg] + h01 * pts[seg + 1].y + h11 * h * tangents[seg + 1];
    return Math.max(0, Math.min(1, y));
  }

  function drawPointCurve() {
    const canvases = document.querySelectorAll('canvas[data-craft-curve]');
    const theme = getTheme(state.themeId);

    canvases.forEach((canvas) => {
      const ctx = canvas.getContext('2d');
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      // Background
      ctx.fillStyle = theme.histogram_bg;
      ctx.fillRect(0, 0, w, h);

      // Synthetic luminance histogram backdrop
      ctx.fillStyle = theme.dark ? 'rgba(255, 255, 255, 0.09)' : 'rgba(0, 0, 0, 0.09)';
      const bins = 64;
      const barW = w / bins;
      for (let i = 0; i < bins; i++) {
        const t = i / bins;
        const v =
          0.55 * Math.exp(-Math.pow((t - 0.34) / 0.16, 2)) +
          0.75 * Math.exp(-Math.pow((t - 0.68) / 0.19, 2)) +
          0.12 * Math.sin(t * 18);
        const bh = Math.max(2, Math.min(h * 0.85, v * h * 0.78));
        ctx.fillRect(i * barW, h - bh, Math.max(1, barW - 1), bh);
      }

      // 4x4 Quarter Gridlines
      ctx.strokeStyle = theme.dark ? 'rgba(255, 255, 255, 0.10)' : 'rgba(0, 0, 0, 0.10)';
      ctx.lineWidth = 1;
      for (let g = 1; g <= 3; g++) {
        const gx = Math.round((w * g) / 4) + 0.5;
        const gy = Math.round((h * g) / 4) + 0.5;
        ctx.beginPath();
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, h);
        ctx.moveTo(0, gy);
        ctx.lineTo(w, gy);
        ctx.stroke();
      }

      // Diagonal Reference Line
      ctx.strokeStyle = theme.dark ? 'rgba(255, 255, 255, 0.20)' : 'rgba(0, 0, 0, 0.20)';
      ctx.beginPath();
      ctx.moveTo(0, h);
      ctx.lineTo(w, 0);
      ctx.stroke();

      // Active Channel Curve Stroke
      const channelColors = {
        rgb: theme.text,
        r: '#ff5f63',
        g: '#52d869',
        b: '#4aa3ff'
      };
      ctx.strokeStyle = channelColors[state.curveChannel] || theme.text;
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      for (let px = 0; px <= w; px += 2) {
        const x = px / w;
        const y = evalMonotoneCubic(state.curvePoints, x);
        const sy = (1 - y) * h;
        if (px === 0) ctx.moveTo(px, sy);
        else ctx.lineTo(px, sy);
      }
      ctx.stroke();

      // Control points (4.5px hollow circle, 6px filled accent circle for active)
      state.curvePoints.forEach((pt, idx) => {
        const cx = pt.x * w;
        const cy = (1 - pt.y) * h;
        ctx.beginPath();
        if (idx === state.activeCurvePoint) {
          ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
          ctx.fillStyle = theme.accent;
          ctx.fill();
          ctx.lineWidth = 1.5;
          ctx.strokeStyle = '#ffffff';
          ctx.stroke();
        } else {
          ctx.arc(cx, cy, 4.2, 0, Math.PI * 2);
          ctx.fillStyle = theme.histogram_bg;
          ctx.fill();
          ctx.lineWidth = 1.5;
          ctx.strokeStyle = theme.text;
          ctx.stroke();
        }
      });
    });

    // Update Input / Output readout if present
    const activePt = state.curvePoints[state.activeCurvePoint] || state.curvePoints[0];
    if (activePt) {
      const inEl = document.getElementById('curve-in-readout');
      const outEl = document.getElementById('curve-out-readout');
      if (inEl) inEl.textContent = String(Math.round(activePt.x * 255));
      if (outEl) outEl.textContent = String(Math.round(activePt.y * 255));
    }
  }

  function bindPointCurveCanvases() {
    document.querySelectorAll('canvas[data-craft-curve]').forEach((canvas) => {
      let draggingIdx = -1;

      function getNormCoords(e) {
        const rect = canvas.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const y = Math.max(0, Math.min(1, 1 - (e.clientY - rect.top) / rect.height));
        return { x, y };
      }

      canvas.addEventListener('pointerdown', (e) => {
        const { x, y } = getNormCoords(e);
        // Find nearest point within radius
        let nearest = -1;
        let bestDist = 0.065;
        state.curvePoints.forEach((pt, idx) => {
          const d = Math.hypot(pt.x - x, pt.y - y);
          if (d < bestDist) {
            bestDist = d;
            nearest = idx;
          }
        });

        if (nearest === -1 && state.curvePoints.length < 16) {
          state.curvePoints.push({ x, y });
          state.curvePoints.sort((a, b) => a.x - b.x);
          nearest = state.curvePoints.findIndex((p) => p.x === x && p.y === y);
        }

        if (nearest !== -1) {
          draggingIdx = nearest;
          state.activeCurvePoint = nearest;
          canvas.setPointerCapture(e.pointerId);
          drawPointCurve();
          drawWorkspaceCanvas();
        }
      });

      canvas.addEventListener('pointermove', (e) => {
        if (draggingIdx === -1) return;
        const { x, y } = getNormCoords(e);
        const pt = state.curvePoints[draggingIdx];
        if (!pt) return;
        const minX = draggingIdx > 0 ? state.curvePoints[draggingIdx - 1].x + 0.02 : 0;
        const maxX = draggingIdx < state.curvePoints.length - 1 ? state.curvePoints[draggingIdx + 1].x - 0.02 : 1;
        pt.x = draggingIdx === 0 ? 0 : draggingIdx === state.curvePoints.length - 1 ? 1 : Math.max(minX, Math.min(maxX, x));
        pt.y = y;
        drawPointCurve();
        drawWorkspaceCanvas();
      });

      const stop = () => {
        draggingIdx = -1;
      };
      canvas.addEventListener('pointerup', stop);
      canvas.addEventListener('pointercancel', stop);

      // Double-click interior point to remove
      canvas.addEventListener('dblclick', (e) => {
        const { x, y } = getNormCoords(e);
        let nearest = -1;
        let bestDist = 0.07;
        state.curvePoints.forEach((pt, idx) => {
          const d = Math.hypot(pt.x - x, pt.y - y);
          if (d < bestDist) {
            bestDist = d;
            nearest = idx;
          }
        });
        if (nearest > 0 && nearest < state.curvePoints.length - 1) {
          state.curvePoints.splice(nearest, 1);
          state.activeCurvePoint = 0;
          drawPointCurve();
          drawWorkspaceCanvas();
        }
      });
    });
  }

  /* ------------------------------------------------------------------------
     9. Multi-Channel RGB Histogram Renderer
     ------------------------------------------------------------------------ */
  function drawHistograms() {
    const theme = getTheme(state.themeId);
    const curveShift = evalMonotoneCubic(state.curvePoints, 0.5) - 0.5;

    document.querySelectorAll('canvas[data-craft-histogram]').forEach((canvas) => {
      const ctx = canvas.getContext('2d');
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      ctx.fillStyle = theme.histogram_bg;
      ctx.fillRect(0, 0, w, h);

      // Quarter gridlines
      ctx.strokeStyle = theme.dark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)';
      for (let g = 1; g <= 3; g++) {
        const gx = Math.round((w * g) / 4) + 0.5;
        ctx.beginPath();
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, h);
        ctx.stroke();
      }

      const channels = [
        { color: 'rgba(255, 82, 82, 0.45)', stroke: '#ff5252', center1: 0.62 + curveShift * 0.25, center2: 0.30 },
        { color: 'rgba(76, 217, 100, 0.40)', stroke: '#4cd964', center1: 0.48 + curveShift * 0.22, center2: 0.71 },
        { color: 'rgba(55, 142, 240, 0.48)', stroke: '#378ef0', center1: 0.36 + curveShift * 0.20, center2: 0.78 }
      ];

      channels.forEach((ch) => {
        ctx.beginPath();
        ctx.moveTo(0, h);
        for (let px = 0; px <= w; px += 2) {
          const t = px / w;
          const amp =
            0.72 * Math.exp(-Math.pow((t - ch.center1) / 0.14, 2)) +
            0.48 * Math.exp(-Math.pow((t - ch.center2) / 0.11, 2)) +
            0.06 * Math.cos(t * 24);
          const y = h - Math.max(2, Math.min(h - 3, amp * (h - 6)));
          ctx.lineTo(px, y);
        }
        ctx.lineTo(w, h);
        ctx.closePath();
        ctx.fillStyle = ch.color;
        ctx.fill();
        ctx.strokeStyle = ch.stroke;
        ctx.lineWidth = 1.1;
        ctx.stroke();
      });
    });
  }

  /* ------------------------------------------------------------------------
     10. Interactive Miniature PhotoCraft Workspace
     ------------------------------------------------------------------------ */
  // Curated 18 primary tool slots for the miniature workspace toolbar
  const WORKSPACE_TOOLS = [
    { id: 'Move', label: 'Move Tool', icon: 'move', key: 'V', flyout: true },
    { id: 'RectMarquee', label: 'Rectangular Marquee Tool', icon: 'square-dashed', key: 'M', flyout: true },
    { id: 'Lasso', label: 'Lasso Tool', icon: 'lasso', key: 'L', flyout: true },
    { id: 'QuickSelection', label: 'Quick Selection Tool', icon: 'quick-selection', key: 'W', flyout: true },
    { id: 'Crop', label: 'Crop Tool', icon: 'crop', key: 'C', flyout: true },
    { id: 'Eyedropper', label: 'Eyedropper Tool', icon: 'pipette', key: 'I', flyout: true },
    { id: 'Remove', label: 'Remove Tool', icon: 'bandage-sparkle', key: 'J', flyout: true },
    { id: 'Brush', label: 'Brush Tool', icon: 'brush', key: 'B', flyout: true },
    { id: 'CloneStamp', label: 'Clone Stamp Tool', icon: 'stamp', key: 'S', flyout: true },
    { id: 'Eraser', label: 'Eraser Tool', icon: 'eraser', key: 'E', flyout: true },
    { id: 'Gradient', label: 'Gradient Tool', icon: 'gradient', key: 'G', flyout: true },
    { id: 'Pen', label: 'Pen Tool', icon: 'pen-tool', key: 'P', flyout: true },
    { id: 'Type', label: 'Horizontal Type Tool', icon: 'type', key: 'T', flyout: true },
    { id: 'PathSelection', label: 'Path Selection Tool', icon: 'mouse-pointer-2', key: 'A', flyout: true },
    { id: 'Rectangle', label: 'Rectangle Tool', icon: 'rectangle-horizontal', key: 'U', flyout: true },
    { id: 'Hand', label: 'Hand Tool', icon: 'hand', key: 'H', flyout: true },
    { id: 'Zoom', label: 'Zoom Tool', icon: 'zoom-in', key: 'Z', flyout: false }
  ];

  function renderWorkspaceToolbar() {
    const grid = document.getElementById('ws-toolbar-grid');
    if (!grid) return;

    grid.innerHTML = WORKSPACE_TOOLS.map(
      (t) => `
      <button type="button" class="craft-tool-btn ${t.id === state.activeTool ? 'is-active' : ''} ${t.flyout ? 'has-flyout' : ''}" data-ws-tool="${t.id}" title="${t.label} (${t.key})">
        <span class="craft-icon" data-craft-icon="${t.icon}"></span>
      </button>`
    ).join('');

    hydrateIcons(grid);

    grid.querySelectorAll('[data-ws-tool]').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.activeTool = btn.getAttribute('data-ws-tool');
        renderWorkspaceToolbar();
        updateWorkspaceToolBadge();
      });
    });
  }

  function updateWorkspaceToolBadge() {
    const tool = WORKSPACE_TOOLS.find((t) => t.id === state.activeTool) || WORKSPACE_TOOLS[7];
    const iconEl = document.getElementById('ws-active-tool-icon');
    const nameEl = document.getElementById('ws-active-tool-name');
    if (iconEl) {
      iconEl.setAttribute('data-craft-icon', tool.icon);
      hydrateIcons(iconEl.parentElement);
    }
    if (nameEl) {
      nameEl.textContent = tool.label.replace(' Tool', '');
    }
  }

  function updateColorChipsUI() {
    document.querySelectorAll('[data-fg-chip]').forEach((el) => {
      el.style.backgroundColor = state.fgColor;
    });
    document.querySelectorAll('[data-bg-chip]').forEach((el) => {
      el.style.backgroundColor = state.bgColor;
    });
    const hexReadout = document.getElementById('ws-fg-hex');
    if (hexReadout) hexReadout.textContent = state.fgColor.toUpperCase();
    drawWorkspaceCanvas();
  }

  function renderWorkspaceLayers() {
    const list = document.getElementById('ws-layer-list');
    if (!list) return;

    list.innerHTML = state.layers
      .map((layer) => {
        const selected = layer.id === state.selectedLayerId;
        const typeBadge =
          layer.kind === 'type'
            ? `<span class="craft-icon craft-icon-sm" data-craft-icon="type"></span>`
            : layer.kind === 'adjustment'
            ? `<span class="craft-icon craft-icon-sm" data-craft-icon="contrast"></span>`
            : '';
        return `
        <div class="craft-layer-row ${selected ? 'is-selected' : ''}" data-layer-id="${layer.id}" data-label-color="${layer.labelColor}">
          <div class="craft-layer-eye ${layer.visible ? '' : 'is-hidden'}" data-layer-eye="${layer.id}" title="Toggle Visibility (Click or drag column)">
            <span class="craft-icon" data-craft-icon="eye"></span>
          </div>
          <div class="craft-layer-main">
            <div class="craft-layer-thumb ${selected ? 'has-focus-corners' : ''}" style="${
              layer.id === 'bg-gradient'
                ? 'background: linear-gradient(135deg, #0f172a, #1e1b4b);'
                : layer.id === 'kitsune-hero'
                ? `background: radial-gradient(circle at 50% 45%, ${state.fgColor}, #1e1b4b 78%);`
                : layer.id === 'kitsune-glow'
                ? 'background: radial-gradient(circle, #f97316, transparent 70%);'
                : ''
            }">
              ${typeBadge ? `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:var(--craft-field);">${typeBadge}</div>` : ''}
            </div>
            ${
              layer.hasMask
                ? `<span class="craft-icon craft-icon-sm" data-craft-icon="link-2" style="opacity:0.7;"></span>
                   <div class="craft-layer-thumb" style="width:22px; background:radial-gradient(circle, #ffffff 45%, #111111 80%);" title="Layer Mask Thumbnail"></div>`
                : ''
            }
            <span class="craft-layer-name">${layer.name}</span>
            <span class="craft-layer-badges">
              ${layer.fx ? `<span title="Layer Effects (fx)" style="font-style:italic; font-weight:600; font-family:serif;">fx</span>` : ''}
              ${layer.locked ? `<span class="craft-icon craft-icon-sm" data-craft-icon="lock"></span>` : ''}
            </span>
          </div>
        </div>`;
      })
      .join('');

    hydrateIcons(list);

    let eyeDragState = null;
    list.querySelectorAll('[data-layer-eye]').forEach((eyeEl) => {
      eyeEl.addEventListener('pointerdown', (e) => {
        e.stopPropagation();
        const id = eyeEl.getAttribute('data-layer-eye');
        const layer = state.layers.find((l) => l.id === id);
        if (!layer) return;
        layer.visible = !layer.visible;
        eyeDragState = layer.visible;
        renderWorkspaceLayers();
        drawWorkspaceCanvas();
      });
      eyeEl.addEventListener('pointerenter', (e) => {
        if (e.buttons !== 1 || eyeDragState === null) return;
        const id = eyeEl.getAttribute('data-layer-eye');
        const layer = state.layers.find((l) => l.id === id);
        if (layer && layer.visible !== eyeDragState) {
          layer.visible = eyeDragState;
          renderWorkspaceLayers();
          drawWorkspaceCanvas();
        }
      });
    });
    window.addEventListener('pointerup', () => {
      eyeDragState = null;
    });

    list.querySelectorAll('.craft-layer-row').forEach((row) => {
      row.addEventListener('click', () => {
        state.selectedLayerId = row.getAttribute('data-layer-id');
        renderWorkspaceLayers();
      });
    });
  }

  function drawWorkspaceCanvas() {
    const canvas = document.getElementById('ws-art-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    // Checkerboard base
    const tile = 12;
    for (let y = 0; y < h; y += tile) {
      for (let x = 0; x < w; x += tile) {
        ctx.fillStyle = (x / tile + y / tile) % 2 === 0 ? '#2b2b2e' : '#222225';
        ctx.fillRect(x, y, tile, tile);
      }
    }

    const bgLayer = state.layers.find((l) => l.id === 'bg-gradient');
    const heroLayer = state.layers.find((l) => l.id === 'kitsune-hero');
    const glowLayer = state.layers.find((l) => l.id === 'kitsune-glow');
    const typeLayer = state.layers.find((l) => l.id === 'type-badge');
    const curvesLayer = state.layers.find((l) => l.id === 'curves-adj');

    // 1. Background Studio Gradient
    if (bgLayer && bgLayer.visible) {
      const grad = ctx.createLinearGradient(0, 0, w, h);
      grad.addColorStop(0, '#0b1021');
      grad.addColorStop(0.55, '#191736');
      grad.addColorStop(1, '#261435');
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, w, h);

      // Subtle perspective synth grid on floor
      ctx.strokeStyle = 'rgba(139, 124, 246, 0.14)';
      ctx.lineWidth = 1;
      const horizon = h * 0.68;
      for (let i = -8; i <= 8; i++) {
        ctx.beginPath();
        ctx.moveTo(w / 2 + i * 18, horizon);
        ctx.lineTo(w / 2 + i * 95, h);
        ctx.stroke();
      }
    }

    // 2. Kitsune Rim Light Glow
    if (glowLayer && glowLayer.visible) {
      ctx.save();
      ctx.globalAlpha = (glowLayer.opacity / 100) * (state.brushOpacity / 100);
      const radGrad = ctx.createRadialGradient(w * 0.5, h * 0.48, 12, w * 0.5, h * 0.48, 155);
      radGrad.addColorStop(0, state.fgColor);
      radGrad.addColorStop(0.45, 'rgba(247, 151, 68, 0.42)');
      radGrad.addColorStop(1, 'rgba(247, 151, 68, 0)');
      ctx.fillStyle = radGrad;
      ctx.beginPath();
      ctx.arc(w * 0.5, h * 0.48, 155, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    // 3. Geometric Kitsune Subject Emblem + Dynamic Brush Ring
    if (heroLayer && heroLayer.visible) {
      ctx.save();
      ctx.translate(w * 0.5, h * 0.49);
      ctx.rotate(((state.brushAngle - 32) * Math.PI) / 360);

      // Left & Right Ears
      ctx.fillStyle = '#f76b3c';
      ctx.beginPath();
      ctx.moveTo(-64, -18);
      ctx.lineTo(-78, -94);
      ctx.lineTo(-18, -52);
      ctx.closePath();
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(64, -18);
      ctx.lineTo(78, -94);
      ctx.lineTo(18, -52);
      ctx.closePath();
      ctx.fill();

      // Inner Ear Highlights
      ctx.fillStyle = state.fgColor;
      ctx.beginPath();
      ctx.moveTo(-56, -26);
      ctx.lineTo(-68, -78);
      ctx.lineTo(-26, -48);
      ctx.closePath();
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(56, -26);
      ctx.lineTo(68, -78);
      ctx.lineTo(26, -48);
      ctx.closePath();
      ctx.fill();

      // Kitsune Face Shield
      const faceGrad = ctx.createLinearGradient(0, -62, 0, 78);
      faceGrad.addColorStop(0, '#ff884b');
      faceGrad.addColorStop(0.65, '#e84f35');
      faceGrad.addColorStop(1, '#b82834');
      ctx.fillStyle = faceGrad;
      ctx.beginPath();
      ctx.moveTo(0, -58);
      ctx.lineTo(72, -12);
      ctx.lineTo(48, 42);
      ctx.lineTo(0, 78);
      ctx.lineTo(-48, 42);
      ctx.lineTo(-72, -12);
      ctx.closePath();
      ctx.fill();

      // White Cheek Facets
      ctx.fillStyle = '#f8fafc';
      ctx.beginPath();
      ctx.moveTo(-72, -12);
      ctx.lineTo(-22, 18);
      ctx.lineTo(0, 78);
      ctx.lineTo(-48, 42);
      ctx.closePath();
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(72, -12);
      ctx.lineTo(22, 18);
      ctx.lineTo(0, 78);
      ctx.lineTo(48, 42);
      ctx.closePath();
      ctx.fill();

      // Glowing Eyes
      ctx.fillStyle = '#ffffff';
      ctx.beginPath();
      ctx.moveTo(-42, -2);
      ctx.lineTo(-16, 6);
      ctx.lineTo(-28, 14);
      ctx.closePath();
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(42, -2);
      ctx.lineTo(16, 6);
      ctx.lineTo(28, 14);
      ctx.closePath();
      ctx.fill();

      // Nose
      ctx.fillStyle = '#0f172a';
      ctx.beginPath();
      ctx.moveTo(-9, 64);
      ctx.lineTo(9, 64);
      ctx.lineTo(0, 78);
      ctx.closePath();
      ctx.fill();

      ctx.restore();
    }

    // 4. Curves Tone Adjustment Overlay
    if (curvesLayer && curvesLayer.visible) {
      const midLift = evalMonotoneCubic(state.curvePoints, 0.5) - 0.5;
      if (Math.abs(midLift) > 0.01) {
        ctx.fillStyle =
          midLift > 0
            ? `rgba(255, 255, 255, ${Math.min(0.28, midLift * 0.45)})`
            : `rgba(0, 0, 0, ${Math.min(0.38, -midLift * 0.55)})`;
        ctx.fillRect(0, 0, w, h);
      }
    }

    // 5. Type Layer + Selection Guides
    if (typeLayer && typeLayer.visible) {
      ctx.fillStyle = 'rgba(255, 255, 255, 0.92)';
      ctx.font = '600 13px Inter, sans-serif';
      ctx.letterSpacing = '2px';
      ctx.fillText('CRAFT // DESIGN SYSTEM', 22, 32);
      ctx.fillStyle = 'rgba(255, 255, 255, 0.55)';
      ctx.font = '400 10.5px "JetBrains Mono", monospace';
      ctx.fillText(`1920 × 1080 px  •  RGBA/8  •  ${getTheme(state.themeId).label}`, 22, 49);
    }

    // Active Tool Cursor / Brush Preview Overlay
    const r = Math.max(8, Math.min(90, state.brushSize * 0.65));
    ctx.save();
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.arc(w * 0.72, h * 0.34, r, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  /* ------------------------------------------------------------------------
     11. Full 161-Icon & 68-Tool Catalog Renderer
     ------------------------------------------------------------------------ */
  function renderIconCatalog(filterText = '') {
    const grid = document.getElementById('ds-icon-catalog');
    const countBadge = document.getElementById('ds-icon-count-badge');
    if (!grid) return;

    const query = filterText.trim().toLowerCase();
    const names = Object.keys(ICONS).sort();
    const filtered = query ? names.filter((n) => n.toLowerCase().includes(query)) : names;

    if (countBadge) {
      countBadge.textContent = `${filtered.length} / ${names.length} SVG icons (stroke-width: 1.75)`;
    }

    grid.innerHTML = filtered
      .map(
        (name) => `
      <div class="ds-icon-cell" data-copy-icon="${name}" title="Click to copy icon name '${name}'">
        <span class="craft-icon">${ICONS[name]}</span>
        <span class="craft-mono craft-elided" style="font-size:11px;">${name}</span>
      </div>`
      )
      .join('');

    grid.querySelectorAll('[data-copy-icon]').forEach((cell) => {
      cell.addEventListener('click', () => {
        const name = cell.getAttribute('data-copy-icon');
        if (navigator.clipboard) navigator.clipboard.writeText(name);
        showToast(`Copied icon '${name}'`);
      });
    });
  }

  /* ------------------------------------------------------------------------
     12. Initialize Everything on DOMContentLoaded
     ------------------------------------------------------------------------ */
  function init() {
    hydrateIcons(document);
    renderThemeGallery();
    renderLayerLabelSwatches();
    renderWorkspaceToolbar();
    updateWorkspaceToolBadge();
    renderWorkspaceLayers();
    bindValueControlGroup(document);
    initAngleDials();
    bindPointCurveCanvases();
    renderIconCatalog('');

    // Populate theme dropdown
    const themeSelect = document.getElementById('ds-theme-select');
    if (themeSelect) {
      themeSelect.innerHTML = SPEC.themes
        .map((t) => `<option value="${t.id}">${t.label} — ${t.family}</option>`)
        .join('');
      themeSelect.value = state.themeId;
      themeSelect.addEventListener('change', () => {
        setTheme(themeSelect.value);
      });
    }

    // Cycle appearance button
    const cycleBtn = document.getElementById('ds-cycle-theme-btn');
    if (cycleBtn) {
      cycleBtn.addEventListener('click', cycleAppearance);
    }

    // UI Font Size buttons
    document.querySelectorAll('[data-font-size-btn]').forEach((btn) => {
      btn.addEventListener('click', () => {
        setFontSize(btn.getAttribute('data-font-size-btn'));
      });
    });

    // Platform dialog button order switch
    document.querySelectorAll('[data-platform-order-btn]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const order = btn.getAttribute('data-platform-order-btn');
        state.platformOrder = order;
        document.querySelectorAll('[data-platform-order-btn]').forEach((b) => {
          b.classList.toggle('is-active', b === btn);
        });
        document.querySelectorAll('.craft-dialog-buttons').forEach((strip) => {
          strip.setAttribute('data-platform-order', order);
        });
      });
    });

    // Checkboxes & Toggles interactive click
    document.addEventListener('click', (e) => {
      const checkRow = e.target.closest('.craft-check-row');
      if (checkRow) {
        const box = checkRow.querySelector('.craft-checkbox, .craft-toggle');
        if (box) box.classList.toggle('is-checked');
        return;
      }
      const box = e.target.closest('.craft-checkbox, .craft-toggle');
      if (box) {
        box.classList.toggle('is-checked');
        return;
      }

      // Close open popup value fields when clicking outside
      if (!e.target.closest('.craft-popup-value-field')) {
        document.querySelectorAll('.craft-popup-value-field.is-open').forEach((f) => f.classList.remove('is-open'));
      }
    });

    // Tab switching inside .craft-tab-strip
    document.querySelectorAll('.craft-tab-strip').forEach((strip) => {
      strip.querySelectorAll('.craft-tab').forEach((tab) => {
        tab.addEventListener('click', () => {
          strip.querySelectorAll('.craft-tab').forEach((t) => t.classList.remove('is-active'));
          tab.classList.add('is-active');
          const targetPane = tab.getAttribute('data-tab-target');
          if (targetPane) {
            const panel = strip.closest('.craft-panel');
            if (panel) {
              panel.querySelectorAll('[data-tab-pane]').forEach((pane) => {
                pane.style.display = pane.getAttribute('data-tab-pane') === targetPane ? 'flex' : 'none';
              });
            }
          }
        });
      });
    });

    // Workspace Toolbar 1-col / 2-col collapse toggle
    const tbCollapse = document.getElementById('ws-toolbar-collapse');
    const wsToolbar = document.getElementById('ws-toolbar');
    if (tbCollapse && wsToolbar) {
      tbCollapse.addEventListener('click', () => {
        state.twoColumnToolbar = !state.twoColumnToolbar;
        wsToolbar.classList.toggle('is-two-column', state.twoColumnToolbar);
        tbCollapse.textContent = state.twoColumnToolbar ? '◂◂' : '▸▸';
      });
    }

    // Workspace Options Bar narrow toggle
    const narrowBtn = document.getElementById('ws-narrow-options-btn');
    const wsOptionsBar = document.getElementById('ws-options-bar');
    if (narrowBtn && wsOptionsBar) {
      narrowBtn.addEventListener('click', () => {
        state.narrowOptionsBar = !state.narrowOptionsBar;
        wsOptionsBar.classList.toggle('is-narrow', state.narrowOptionsBar);
        narrowBtn.classList.toggle('is-active', state.narrowOptionsBar);
      });
    }

    // Color Chips Swap (X) and Default (D)
    document.querySelectorAll('[data-action-swap-colors]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tmp = state.fgColor;
        state.fgColor = state.bgColor;
        state.bgColor = tmp;
        updateColorChipsUI();
        showToast(`Swapped FG/BG (${state.fgColor} ⇄ ${state.bgColor})`);
      });
    });

    document.querySelectorAll('[data-action-default-colors]').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.fgColor = '#000000';
        state.bgColor = '#ffffff';
        updateColorChipsUI();
        showToast('Reset FG/BG to Default (#000000 / #FFFFFF)');
      });
    });

    // Swatch clicks (a swatch's colour is data: `data-swatch-color` paints it)
    document.querySelectorAll('.craft-swatch[data-swatch-color]').forEach((sw) => {
      sw.style.backgroundColor = sw.getAttribute('data-swatch-color');
      sw.addEventListener('click', () => {
        document.querySelectorAll('.craft-swatch').forEach((s) => s.classList.remove('is-selected'));
        sw.classList.add('is-selected');
        state.fgColor = sw.getAttribute('data-swatch-color');
        updateColorChipsUI();
      });
    });

    // Workspace Brush Size & Opacity sliders linked to canvas
    const brushSizeSlider = document.getElementById('ws-brush-size-slider');
    if (brushSizeSlider) {
      brushSizeSlider.addEventListener('craft-change', (e) => {
        state.brushSize = e.detail.value;
        drawWorkspaceCanvas();
      });
    }
    const brushOpacitySlider = document.getElementById('ws-brush-opacity-slider');
    if (brushOpacitySlider) {
      brushOpacitySlider.addEventListener('craft-change', (e) => {
        state.brushOpacity = e.detail.value;
        drawWorkspaceCanvas();
      });
    }

    // Curves Presets & Channel selector
    document.querySelectorAll('[data-curve-preset]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const preset = btn.getAttribute('data-curve-preset');
        if (preset === 'linear') {
          state.curvePoints = [
            { x: 0, y: 0 },
            { x: 1, y: 1 }
          ];
        } else if (preset === 'scurve') {
          state.curvePoints = [
            { x: 0, y: 0 },
            { x: 0.25, y: 0.16 },
            { x: 0.75, y: 0.85 },
            { x: 1, y: 1 }
          ];
        } else if (preset === 'matte') {
          state.curvePoints = [
            { x: 0, y: 0.12 },
            { x: 0.3, y: 0.26 },
            { x: 0.72, y: 0.82 },
            { x: 1, y: 0.94 }
          ];
        }
        state.activeCurvePoint = Math.min(1, state.curvePoints.length - 1);
        drawPointCurve();
        drawHistograms();
        drawWorkspaceCanvas();
      });
    });

    document.querySelectorAll('[data-curve-channel]').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.curveChannel = btn.getAttribute('data-curve-channel');
        document.querySelectorAll('[data-curve-channel]').forEach((b) => b.classList.toggle('is-active', b === btn));
        drawPointCurve();
      });
    });

    // Interactive Math Expression Tester
    const mathInput = document.getElementById('ds-math-tester-input');
    const mathOutput = document.getElementById('ds-math-tester-output');
    if (mathInput && mathOutput) {
      const updateMath = () => {
        const res = evalArithmetic(mathInput.value);
        mathOutput.textContent = res !== null ? `= ${Number(res.toFixed(4))}` : 'Invalid expression';
        mathOutput.style.color = res !== null ? 'var(--craft-accent)' : 'var(--craft-danger)';
      };
      mathInput.addEventListener('input', updateMath);
      document.querySelectorAll('[data-math-preset]').forEach((chip) => {
        chip.addEventListener('click', () => {
          mathInput.value = chip.getAttribute('data-math-preset');
          updateMath();
        });
      });
      updateMath();
    }

    // Icon Search Filter
    const iconSearch = document.getElementById('ds-icon-search');
    if (iconSearch) {
      iconSearch.addEventListener('input', () => {
        renderIconCatalog(iconSearch.value);
      });
    }

    // Modal Dialog Open / Close
    document.querySelectorAll('[data-open-modal]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-open-modal');
        const modal = document.getElementById(id);
        if (modal) modal.classList.add('is-open');
      });
    });
    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const modal = btn.closest('.craft-modal-backdrop');
        if (modal) modal.classList.remove('is-open');
      });
    });
    document.querySelectorAll('.craft-modal-backdrop').forEach((backdrop) => {
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) backdrop.classList.remove('is-open');
      });
    });

    // Apply initial theme and UI font size. Priority: ?theme= / ?font= query, then whatever is
    // already on <html> (a host page or the screenshot tool may set it), then the kit default.
    const params = new URLSearchParams(window.location.search);
    const htmlEl = document.documentElement;
    const urlTheme = params.get('theme') || htmlEl.getAttribute('data-craft-theme');
    if (urlTheme && SPEC.themes.some((t) => t.id === urlTheme)) {
      state.themeId = urlTheme;
    }
    const urlFont = params.get('font') || htmlEl.getAttribute('data-craft-font-size');
    if (urlFont && ['tiny', 'small', 'medium', 'large'].includes(urlFont)) {
      state.fontSize = urlFont;
    }
    setFontSize(state.fontSize);
    setTheme(state.themeId);
    updateColorChipsUI();
  }

  window.CraftDS = {
    setTheme,
    cycleAppearance,
    setFontSize,
    evalArithmetic,
    state
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
