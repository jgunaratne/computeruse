/**
 * Translate browser input events into the computer's action vocabulary
 * (`computeruse/computer/actions.py`), for operator control of a session.
 *
 * Pure functions, no DOM access: the caller passes the relevant event fields.
 */
import type { InputAction } from "../types";

export type Modifiers = { ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean };

/** `KeyboardEvent.key` values that are not printable characters, in X11 keysym names. */
const NAMED_KEYS: Record<string, string> = {
  Enter: "Return",
  Backspace: "BackSpace",
  Tab: "Tab",
  Escape: "Escape",
  Delete: "Delete",
  Insert: "Insert",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Home: "Home",
  End: "End",
  PageUp: "Page_Up",
  PageDown: "Page_Down",
  CapsLock: "Caps_Lock",
  ContextMenu: "Menu",
  PrintScreen: "Print",
  " ": "space",
};
for (let i = 1; i <= 24; i++) NAMED_KEYS[`F${i}`] = `F${i}`;

/** Printable characters that need a name inside a chord ("ctrl++" is not parseable). */
const CHAR_NAMES: Record<string, string> = {
  "+": "plus",
  "-": "minus",
  "=": "equal",
  ",": "comma",
  ".": "period",
  "/": "slash",
  "\\": "backslash",
  ";": "semicolon",
  "'": "apostrophe",
  "`": "grave",
  "[": "bracketleft",
  "]": "bracketright",
};

const MODIFIER_KEYS = new Set(["Control", "Alt", "Shift", "Meta", "OS", "AltGraph", "Hyper", "Super", "Fn", "FnLock", "NumLock", "ScrollLock"]);

export const isModifierKey = (key: string): boolean => MODIFIER_KEYS.has(key);

/**
 * Modifier prefix in xdotool syntax. Cmd on a Mac maps to ctrl: the computer being driven is a
 * Linux desktop/browser where ctrl is the primary modifier, so Cmd+L / Cmd+T do what the user means.
 */
export function modifierChord(m: Modifiers, includeShift = true): string[] {
  const out: string[] = [];
  if (m.ctrlKey || m.metaKey) out.push("ctrl");
  if (m.altKey) out.push("alt");
  if (includeShift && m.shiftKey) out.push("shift");
  return out;
}

/**
 * Map a keydown to an action: `type` for plain printable characters, `key` for named keys and
 * chords, null for events that should be ignored (lone modifiers, IME composition, dead keys).
 */
export function keyAction(e: { key: string } & Modifiers): InputAction | null {
  const { key } = e;
  if (!key || isModifierKey(key) || key === "Dead" || key === "Process" || key === "Unidentified" || key === "Compose") return null;
  const named = NAMED_KEYS[key];
  if (named) {
    return { action: "key", text: [...modifierChord(e), named].join("+") };
  }
  if ([...key].length !== 1) return null; // other special keys (MediaPlay, BrowserBack, ...)
  const plain = !e.ctrlKey && !e.altKey && !e.metaKey;
  if (plain) return { action: "type", text: key }; // `key` already reflects Shift/CapsLock ("T", "!")
  // Chord with a printable key: send the unshifted base plus an explicit shift so "ctrl+shift+t" reads right.
  let base = key;
  const mods = modifierChord(e, false);
  if (/^[A-Z]$/.test(base)) {
    base = base.toLowerCase();
    mods.push("shift");
  } else if (e.shiftKey && /^[a-z]$/.test(base)) {
    mods.push("shift");
  } else if (e.shiftKey && !CHAR_NAMES[base] && base === base.toUpperCase() && base !== base.toLowerCase()) {
    mods.push("shift");
  }
  base = CHAR_NAMES[base] ?? base;
  return { action: "key", text: [...mods, base].join("+") };
}

/** Accumulates wheel deltas into whole scroll "clicks" (one X11 wheel step ≈ 50 CSS px). */
export class WheelAccumulator {
  private x = 0;
  private y = 0;
  constructor(private readonly stepPx = 50, private readonly maxPerEvent = 10) {}

  /** Returns 0–2 scroll actions for a wheel event at the given screen coordinate. */
  push(e: { deltaX: number; deltaY: number; deltaMode: number } & Modifiers, coordinate: [number, number]): InputAction[] {
    const unit = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 400 : 1; // lines / pages → px
    this.x += e.deltaX * unit;
    this.y += e.deltaY * unit;
    const out: InputAction[] = [];
    const mods = modifierChord(e).join("+") || undefined;
    const ny = Math.trunc(this.y / this.stepPx);
    if (ny) {
      this.y -= ny * this.stepPx;
      out.push({ action: "scroll", coordinate, scroll_direction: ny > 0 ? "down" : "up", scroll_amount: Math.min(this.maxPerEvent, Math.abs(ny)), ...(mods ? { text: mods } : {}) });
    }
    const nx = Math.trunc(this.x / this.stepPx);
    if (nx) {
      this.x -= nx * this.stepPx;
      out.push({ action: "scroll", coordinate, scroll_direction: nx > 0 ? "right" : "left", scroll_amount: Math.min(this.maxPerEvent, Math.abs(nx)), ...(mods ? { text: mods } : {}) });
    }
    return out;
  }

  reset(): void {
    this.x = this.y = 0;
  }
}

/** Convert a client-space point inside `rect` to display pixels, clamped to the display. */
export function toDisplayCoordinate(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  display: { width: number; height: number },
): [number, number] {
  const x = Math.round(((clientX - rect.left) / Math.max(1, rect.width)) * display.width);
  const y = Math.round(((clientY - rect.top) / Math.max(1, rect.height)) * display.height);
  return [Math.min(display.width - 1, Math.max(0, x)), Math.min(display.height - 1, Math.max(0, y))];
}

/** Click action for a non-left button (right/middle), with modifiers as the xdotool-style `text`. */
export function buttonClick(button: number, coordinate: [number, number], m: Modifiers): InputAction | null {
  const kind = button === 2 ? "right_click" : button === 1 ? "middle_click" : null;
  if (!kind) return null;
  const mods = modifierChord(m).join("+");
  return { action: kind, coordinate, ...(mods ? { text: mods } : {}) };
}
