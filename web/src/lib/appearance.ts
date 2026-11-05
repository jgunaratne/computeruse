/**
 * Craft Design System runtime bridge for computeruse:
 *  - Syncs `data-craft-theme` (all 14 themes across Pro, Studio, Classic) and `data-craft-font-size`
 *    (`tiny` | `small` | `medium` | `large`) with `?theme=` / `?font=` URL query params first and
 *    `localStorage` second, delegating flag management (`data-craft-pro`, `data-craft-bevel`,
 *    `data-craft-round-chips`, `data-craft-dark`) to `window.CraftDS`.
 *  - Hydrates `[data-craft-icon]` spans dynamically whenever React mounts or updates DOM nodes.
 */

import { useEffect, useState } from "react";

export type CraftThemeId =
  | "pro"
  | "proMedium"
  | "proLightGray"
  | "proLight"
  | "studio"
  | "studioLight"
  | "solarizedDark"
  | "adwaita"
  | "adwaitaDark"
  | "breezeLight"
  | "breezeDark"
  | "softDark"
  | "softLight"
  | "classic";

export type CraftFontSize = "tiny" | "small" | "medium" | "large";

export interface CraftThemeOption {
  id: CraftThemeId;
  label: string;
  grammar: "Pro" | "Studio" | "Classic";
}

export const CRAFT_THEME_GROUPS: { grammar: "Pro" | "Studio" | "Classic"; items: CraftThemeOption[] }[] = [
  {
    grammar: "Pro",
    items: [
      { id: "proMedium", label: "Pro (Medium Gray)", grammar: "Pro" },
      { id: "pro", label: "Pro (Dark)", grammar: "Pro" },
      { id: "proLightGray", label: "Pro (Light Gray)", grammar: "Pro" },
      { id: "proLight", label: "Pro (Light)", grammar: "Pro" },
    ],
  },
  {
    grammar: "Studio",
    items: [
      { id: "studio", label: "Studio (Dark)", grammar: "Studio" },
      { id: "studioLight", label: "Studio (Light)", grammar: "Studio" },
      { id: "solarizedDark", label: "Solarized Dark", grammar: "Studio" },
      { id: "adwaita", label: "Adwaita (Light)", grammar: "Studio" },
      { id: "adwaitaDark", label: "Adwaita (Dark)", grammar: "Studio" },
      { id: "breezeLight", label: "Breeze Light", grammar: "Studio" },
      { id: "breezeDark", label: "Breeze Dark", grammar: "Studio" },
      { id: "softDark", label: "Soft Dark", grammar: "Studio" },
      { id: "softLight", label: "Soft Light", grammar: "Studio" },
    ],
  },
  {
    grammar: "Classic",
    items: [{ id: "classic", label: "Classic", grammar: "Classic" }],
  },
];

export const CRAFT_THEMES: CraftThemeOption[] = CRAFT_THEME_GROUPS.flatMap((g) => g.items);

export const CRAFT_FONT_SIZES: { id: CraftFontSize; short: string; label: string }[] = [
  { id: "tiny", short: "XS", label: "Tiny (10px)" },
  { id: "small", short: "S", label: "Small (12px)" },
  { id: "medium", short: "M", label: "Medium (14px)" },
  { id: "large", short: "L", label: "Large (16px)" },
];

const THEME_IDS = new Set<string>(CRAFT_THEMES.map((t) => t.id));
const FONT_IDS = new Set<string>(CRAFT_FONT_SIZES.map((f) => f.id));

const STORAGE_THEME = "cu:craft:theme";
const STORAGE_FONT = "cu:craft:font";
const APPEARANCE_EVENT = "cu:craft-appearance";

declare global {
  interface Window {
    CRAFT_ICONS?: Record<string, string>;
    CraftDS?: {
      setTheme: (themeId: string) => void;
      setFontSize: (size: string) => void;
      cycleAppearance?: () => void;
      evalArithmetic?: (input: string) => number | null;
    };
  }
}

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private browsing */
  }
}

let sessionQueryTheme: CraftThemeId | null = null;
let sessionQueryFont: CraftFontSize | null = null;

export function resolveTheme(): CraftThemeId {
  const fromQuery = new URLSearchParams(window.location.search).get("theme");
  if (fromQuery && THEME_IDS.has(fromQuery)) {
    sessionQueryTheme = fromQuery as CraftThemeId;
    return sessionQueryTheme;
  }
  if (sessionQueryTheme) return sessionQueryTheme;
  const saved = readStorage(STORAGE_THEME);
  if (saved && THEME_IDS.has(saved)) return saved as CraftThemeId;
  return "proMedium";
}

export function resolveFontSize(): CraftFontSize {
  const fromQuery = new URLSearchParams(window.location.search).get("font");
  if (fromQuery && FONT_IDS.has(fromQuery)) {
    sessionQueryFont = fromQuery as CraftFontSize;
    return sessionQueryFont;
  }
  if (sessionQueryFont) return sessionQueryFont;
  const saved = readStorage(STORAGE_FONT);
  if (saved && FONT_IDS.has(saved)) return saved as CraftFontSize;
  return "small";
}

const FALLBACK_FLAGS: Record<CraftThemeId, { pro: boolean; bevel: boolean; roundChips: boolean; dark: boolean }> = {
  pro: { pro: true, bevel: false, roundChips: false, dark: true },
  proMedium: { pro: true, bevel: false, roundChips: false, dark: true },
  proLightGray: { pro: true, bevel: false, roundChips: false, dark: false },
  proLight: { pro: true, bevel: false, roundChips: false, dark: false },
  studio: { pro: false, bevel: false, roundChips: true, dark: true },
  studioLight: { pro: false, bevel: false, roundChips: true, dark: false },
  classic: { pro: false, bevel: true, roundChips: false, dark: false },
  solarizedDark: { pro: false, bevel: false, roundChips: true, dark: true },
  adwaita: { pro: false, bevel: false, roundChips: true, dark: false },
  adwaitaDark: { pro: false, bevel: false, roundChips: true, dark: true },
  breezeLight: { pro: false, bevel: false, roundChips: true, dark: false },
  breezeDark: { pro: false, bevel: false, roundChips: true, dark: true },
  softDark: { pro: false, bevel: false, roundChips: true, dark: true },
  softLight: { pro: false, bevel: false, roundChips: true, dark: false },
};

export function applyCraftTheme(theme: CraftThemeId, persist = true) {
  if (!THEME_IDS.has(theme)) return;
  if (document.body && window.CraftDS?.setTheme) {
    window.CraftDS.setTheme(theme);
  } else {
    const flags = FALLBACK_FLAGS[theme];
    const root = document.documentElement;
    root.dataset.craftTheme = theme;
    root.dataset.craftPro = String(flags.pro);
    root.dataset.craftBevel = String(flags.bevel);
    root.dataset.craftRoundChips = String(flags.roundChips);
    root.dataset.craftDark = String(flags.dark);
  }
  if (persist) writeStorage(STORAGE_THEME, theme);
  window.dispatchEvent(new Event(APPEARANCE_EVENT));
}

export function applyCraftFontSize(size: CraftFontSize, persist = true) {
  if (!FONT_IDS.has(size)) return;
  if (document.body && window.CraftDS?.setFontSize) {
    window.CraftDS.setFontSize(size);
  } else {
    document.documentElement.dataset.craftFontSize = size;
  }
  if (persist) writeStorage(STORAGE_FONT, size);
  window.dispatchEvent(new Event(APPEARANCE_EVENT));
}

function hydrateElementIcon(el: Element) {
  const name = el.getAttribute("data-craft-icon");
  if (!name) return;
  const svg = window.CRAFT_ICONS?.[name];
  if (!svg) return;
  if (el.getAttribute("data-hydrated-icon") === name && el.childElementCount > 0) return;
  el.innerHTML = svg;
  el.setAttribute("data-hydrated-icon", name);
}

function hydrateSubtreeIcons(root: ParentNode) {
  if (!window.CRAFT_ICONS) return;
  if (root instanceof Element && root.hasAttribute("data-craft-icon")) {
    hydrateElementIcon(root);
  }
  root.querySelectorAll("[data-craft-icon]").forEach(hydrateElementIcon);
}

let initialized = false;

export function initCraftAppearance() {
  const syncFromLocation = () => {
    const theme = resolveTheme();
    const font = resolveFontSize();
    applyCraftTheme(theme, sessionQueryTheme === null);
    applyCraftFontSize(font, sessionQueryFont === null);
  };

  syncFromLocation();
  hydrateSubtreeIcons(document);

  if (initialized) return;
  initialized = true;

  window.addEventListener("popstate", syncFromLocation);
  window.addEventListener("cu:navigate", syncFromLocation);
  window.addEventListener("DOMContentLoaded", () => {
    syncFromLocation();
    hydrateSubtreeIcons(document);
  });

  if (typeof MutationObserver !== "undefined") {
    const obs = new MutationObserver((records) => {
      for (const rec of records) {
        if (rec.type === "attributes" && rec.target instanceof Element) {
          hydrateElementIcon(rec.target);
        } else if (rec.type === "childList") {
          rec.addedNodes.forEach((n) => {
            if (n instanceof Element) hydrateSubtreeIcons(n);
          });
        }
      }
    });
    const attach = () => {
      if (!document.body) return;
      hydrateSubtreeIcons(document.body);
      obs.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["data-craft-icon"],
      });
    };
    if (document.body) attach();
    else window.addEventListener("DOMContentLoaded", attach, { once: true });
  }
}

export function useCraftAppearance() {
  const [theme, setThemeState] = useState<CraftThemeId>(() =>
    (document.documentElement.dataset.craftTheme as CraftThemeId) || resolveTheme(),
  );
  const [fontSize, setFontSizeState] = useState<CraftFontSize>(() =>
    (document.documentElement.dataset.craftFontSize as CraftFontSize) || resolveFontSize(),
  );

  useEffect(() => {
    const onSync = () => {
      const t = document.documentElement.dataset.craftTheme as CraftThemeId | undefined;
      const f = document.documentElement.dataset.craftFontSize as CraftFontSize | undefined;
      if (t && THEME_IDS.has(t)) setThemeState(t);
      if (f && FONT_IDS.has(f)) setFontSizeState(f);
    };
    window.addEventListener(APPEARANCE_EVENT, onSync);
    return () => window.removeEventListener(APPEARANCE_EVENT, onSync);
  }, []);

  return {
    theme,
    fontSize,
    setTheme: (next: CraftThemeId) => {
      sessionQueryTheme = null;
      applyCraftTheme(next, true);
    },
    setFontSize: (next: CraftFontSize) => {
      sessionQueryFont = null;
      applyCraftFontSize(next, true);
    },
  };
}
