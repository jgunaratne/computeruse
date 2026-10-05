import { useCallback, useEffect, useState } from "react";

export type Route =
  | { name: "sessions" }
  | { name: "session"; id: string }
  | { name: "metrics" }
  | { name: "evals"; runId?: string };

export function parseRoute(pathname: string): Route {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "sessions" && parts[1]) return { name: "session", id: parts[1] };
  if (parts[0] === "metrics") return { name: "metrics" };
  if (parts[0] === "evals") return { name: "evals", runId: parts[1] };
  return { name: "sessions" };
}

const NAV_EVENT = "cu:navigate";

export function navigate(path: string, replace = false) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  window.dispatchEvent(new Event(NAV_EVENT));
}

export function useRoute(): [Route, (path: string) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(location.pathname));
    window.addEventListener("popstate", onChange);
    window.addEventListener(NAV_EVENT, onChange);
    return () => {
      window.removeEventListener("popstate", onChange);
      window.removeEventListener(NAV_EVENT, onChange);
    };
  }, []);
  return [route, useCallback((p: string) => navigate(p), [])];
}

/** Anchor that does client-side navigation but still works with middle-click / copy link. */
export function Link(props: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  const { to, onClick, ...rest } = props;
  return (
    <a
      href={to}
      {...rest}
      onClick={(e) => {
        onClick?.(e);
        if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        navigate(to);
      }}
    />
  );
}
