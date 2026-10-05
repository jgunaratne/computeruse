import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent, type PointerEvent } from "react";
import type { Step } from "../lib/timeline";
import { classNames } from "../lib/format";
import { WheelAccumulator, buttonClick, keyAction, modifierChord, toDisplayCoordinate } from "../lib/input";
import type { InputAction } from "../types";

export interface ScreenViewProps {
  display: { width: number; height: number } | null;
  /** Image to show (frame URL or live preview data URL). */
  src: string | null;
  live: boolean;
  step: Step | null;
  /** True when the shown image is the frame *before* the step (overlay makes sense). */
  showOverlay: boolean;
  /** The operator holds control and the agent is parked: the screen captures mouse + keyboard. */
  control: boolean;
  /** Control was requested but the agent has not reached its next checkpoint yet. */
  controlPending?: boolean;
  onInput?: (action: InputAction, id?: number) => void;
  onReleaseControl?: () => void;
  caption?: { label: string; text: string } | null;
  placeholder?: string;
}

/** Hold Esc this long to hand control back (a tap is forwarded to the computer). */
export const ESC_HOLD_MS = 900;
const MOVE_THROTTLE_MS = 25;

const KIND_LABEL: Record<string, string> = {
  left_click: "click",
  right_click: "right click",
  middle_click: "middle click",
  double_click: "double click",
  triple_click: "triple click",
  mouse_move: "move",
  left_mouse_down: "mouse down",
  left_mouse_up: "mouse up",
  left_click_drag: "drag",
  scroll: "scroll",
  type: "type",
  key: "key",
  hold_key: "hold key",
  wait: "wait",
  screenshot: "screenshot",
  cursor_position: "cursor position",
};

export function ScreenView(p: ScreenViewProps) {
  const w = p.display?.width ?? 1280;
  const h = p.display?.height ?? 800;
  const unit = Math.max(12, w * 0.012);

  const innerRef = useRef<HTMLDivElement>(null);
  const controlRef = useRef(p.control);
  const onInputRef = useRef(p.onInput);
  controlRef.current = p.control;
  onInputRef.current = p.onInput;

  const idRef = useRef(0);
  const leftDown = useRef(false);
  const deferredLeft = useRef<string | null>(null); // modifiers of a modified left click, sent on release
  const pendingMove = useRef<[number, number] | null>(null);
  const moveTimer = useRef<number | null>(null);
  const wheel = useRef(new WheelAccumulator());
  const escTimer = useRef<number | null>(null);
  const escFired = useRef(false);
  const [focused, setFocused] = useState(false);

  const nextId = () => ++idRef.current;
  const send = useCallback((action: InputAction, id?: number) => onInputRef.current?.(action, id), []);
  const coord = useCallback(
    (e: { clientX: number; clientY: number }): [number, number] => {
      const rect = innerRef.current?.getBoundingClientRect() ?? { left: 0, top: 0, width: w, height: h };
      return toDisplayCoordinate(e.clientX, e.clientY, rect, { width: w, height: h });
    },
    [w, h],
  );

  // Entering control: grab keyboard focus. Leaving it: forget any half-finished gesture.
  useEffect(() => {
    if (p.control) {
      innerRef.current?.focus({ preventScroll: true });
      return;
    }
    leftDown.current = false;
    deferredLeft.current = null;
    pendingMove.current = null;
    wheel.current.reset();
    if (moveTimer.current != null) window.clearTimeout(moveTimer.current);
    if (escTimer.current != null) window.clearTimeout(escTimer.current);
    moveTimer.current = escTimer.current = null;
  }, [p.control]);

  // Wheel must be a native, non-passive listener to stop the page from scrolling.
  useEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!controlRef.current) return;
      e.preventDefault();
      for (const a of wheel.current.push(e, coord(e))) send(a);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [coord, send]);

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (!p.control || e.pointerType === "touch") return;
    e.preventDefault();
    innerRef.current?.focus({ preventScroll: true });
    const c = coord(e);
    if (e.button === 0) {
      const mods = modifierChord(e).join("+");
      if (mods) {
        deferredLeft.current = mods; // shift/ctrl-click: sent as one modified click on release
        return;
      }
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* unsupported */
      }
      leftDown.current = true;
      send({ action: "left_mouse_down", coordinate: c });
    } else {
      const a = buttonClick(e.button, c, e);
      if (a) send(a, nextId());
    }
  };

  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!p.control || e.pointerType === "touch") return;
    pendingMove.current = coord(e);
    if (moveTimer.current == null) {
      moveTimer.current = window.setTimeout(() => {
        moveTimer.current = null;
        const m = pendingMove.current;
        pendingMove.current = null;
        if (m && controlRef.current) send({ action: "mouse_move", coordinate: m });
      }, MOVE_THROTTLE_MS);
    }
  };

  const onPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!p.control || e.pointerType === "touch" || e.button !== 0) return;
    e.preventDefault();
    const c = coord(e);
    if (deferredLeft.current) {
      send({ action: "left_click", coordinate: c, text: deferredLeft.current }, nextId());
      deferredLeft.current = null;
    } else if (leftDown.current) {
      leftDown.current = false;
      send({ action: "left_mouse_up", coordinate: c }, nextId());
    }
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* not captured */
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!p.control) return;
    if (e.key === "Escape") {
      e.preventDefault();
      if (e.repeat || escTimer.current != null) return;
      escFired.current = false;
      escTimer.current = window.setTimeout(() => {
        escTimer.current = null;
        escFired.current = true;
        p.onReleaseControl?.();
      }, ESC_HOLD_MS);
      return;
    }
    const primary = (e.ctrlKey || e.metaKey) && !e.altKey;
    if (primary && !e.shiftKey && e.key.toLowerCase() === "v") return; // let the browser raise `paste` (local clipboard)
    e.preventDefault();
    if (primary && e.shiftKey && e.key.toLowerCase() === "v") {
      send({ action: "key", text: "ctrl+v" }, nextId()); // paste the *computer's* clipboard
      return;
    }
    const a = keyAction(e);
    if (a) send(a, a.action === "key" ? nextId() : undefined);
  };

  const onKeyUp = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!p.control || e.key !== "Escape") return;
    e.preventDefault();
    if (escTimer.current != null) {
      window.clearTimeout(escTimer.current);
      escTimer.current = null;
      if (!escFired.current) send({ action: "key", text: "Escape" }, nextId()); // a tap, not a hold
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLDivElement>) => {
    if (!p.control) return;
    e.preventDefault();
    const text = e.clipboardData.getData("text/plain");
    if (text) send({ action: "type", text }, nextId());
    else send({ action: "key", text: "ctrl+v" }, nextId()); // nothing local to paste: use the computer's clipboard
  };

  const overlay = useMemo(() => {
    const s = p.step;
    if (!s || !p.showOverlay) return null;
    const coords = s.coordinates ?? [];
    const last = coords[coords.length - 1];
    const label = describeForOverlay(s);
    const els: JSX.Element[] = [];
    if (s.kind === "left_click_drag" && coords.length >= 2) {
      const [a, b] = coords;
      els.push(<line key="l" className="marker-line" x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} />);
      els.push(<circle key="a" className="marker-dot" cx={a[0]} cy={a[1]} r={unit * 0.4} />);
      els.push(<circle key="b" className="marker-ring" cx={b[0]} cy={b[1]} r={unit} />);
    } else if (last) {
      els.push(<circle key="r" className="marker-ring" cx={last[0]} cy={last[1]} r={unit} />);
      els.push(<circle key="d" className="marker-dot" cx={last[0]} cy={last[1]} r={unit * 0.22} />);
      if (s.kind === "scroll") {
        const dir = String(s.action.scroll_direction ?? "down");
        const len = unit * 2.2;
        const dx = dir === "left" ? -len : dir === "right" ? len : 0;
        const dy = dir === "up" ? -len : dir === "down" ? len : 0;
        els.push(
          <line key="s" className="marker-line" x1={last[0]} y1={last[1]} x2={last[0] + dx} y2={last[1] + dy} strokeDasharray="0" />,
        );
        els.push(<circle key="s2" className="marker-dot" cx={last[0] + dx} cy={last[1] + dy} r={unit * 0.3} />);
      }
    }
    if (label) {
      const anchor = last ?? [unit * 1.5, h - unit * 1.5];
      const tx = Math.min(w - unit * 1.2, Math.max(unit * 1.2, anchor[0] + unit * 1.3));
      const ty = Math.min(h - unit * 0.8, Math.max(unit * 1.4, anchor[1] - unit * 1.3));
      els.push(
        <text key="t" className="marker-label" x={tx} y={ty} style={{ fontSize: unit * 1.05 }}>
          {label}
        </text>,
      );
    }
    return els;
  }, [p.step, p.showOverlay, unit, w, h]);

  const banner = p.control ? (
    <div className="control-banner on" role="status">
      <span className="dot" />
      {focused ? "You're in control" : "You're in control — click the screen to type"}
      <span className="sep">·</span>
      hold <kbd>Esc</kbd> to hand back
    </div>
  ) : p.controlPending ? (
    <div className="control-banner" role="status">
      <span className="spinner" /> Waiting for the agent to finish its current step…
    </div>
  ) : null;

  return (
    <div className={classNames("screen", p.control && "control", p.controlPending && "control-pending")}>
      <div
        ref={innerRef}
        className="screen-inner"
        style={{ aspectRatio: `${w} / ${h}`, touchAction: p.control ? "none" : undefined }}
        tabIndex={p.control ? 0 : -1}
        aria-label={p.control ? "Computer screen — you are in control; your mouse and keyboard act on it" : "Computer screen"}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onContextMenu={(e) => p.control && e.preventDefault()}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        onPaste={onPaste}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onDragStart={(e) => e.preventDefault()}
      >
        {p.src ? (
          <img src={p.src} alt="Computer screen" draggable={false} />
        ) : (
          <div className="placeholder">
            <span>{p.placeholder ?? "Waiting for the first screenshot…"}</span>
          </div>
        )}
        <svg className="overlay" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
          {overlay}
        </svg>
        {banner}
        {p.caption && !p.control && (
          <div className="screen-caption">
            <div className="caption">
              <div className="k">{p.caption.label}</div>
              <div>{p.caption.text}</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function describeForOverlay(s: Step): string {
  const a = s.action;
  const kind = KIND_LABEL[s.kind] ?? s.kind;
  switch (s.kind) {
    case "type":
      return `type "${truncate(String(a.text ?? ""), 40)}"`;
    case "key":
    case "hold_key":
      return `${kind} ${String(a.text ?? "")}`;
    case "scroll":
      return `scroll ${String(a.scroll_direction ?? "")} ×${String(a.scroll_amount ?? "")}`;
    case "wait":
      return `wait ${String(a.duration ?? "")}s`;
    case "screenshot":
      return "";
    default:
      return kind;
  }
}

const truncate = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s).replace(/\n/g, "⏎");
