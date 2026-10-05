#!/usr/bin/env bash
# Boots a virtual display, Chromium, the control daemon and (optionally) a
# browser-viewable VNC live view. Everything runs as the unprivileged `agent` user.
set -euo pipefail

: "${SCREEN_SIZE:=1280x800}"
: "${DAEMON_PORT:=8800}"
: "${NOVNC_PORT:=6080}"
: "${START_URL:=about:blank}"
: "${ENABLE_VNC:=1}"
# Mount a host directory here (and make it writable by the `agent` user) to keep cookies/logins
# between containers; the default is a throwaway profile inside the container.
: "${PROFILE_DIR:=/tmp/chrome-profile}"
export DISPLAY=:99

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 "${SCREEN_SIZE}x24" -nolisten tcp -ac &
for _ in $(seq 1 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

if [ "$ENABLE_VNC" = "1" ]; then
  x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 -localhost &
  websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 >/dev/null 2>&1 &
  echo "live view: http://<host>:${NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale"
fi

# A profile left behind by a killed Chromium still carries its single-instance lock.
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonSocket" "$PROFILE_DIR/SingletonCookie" 2>/dev/null || true

# The daemon controls the existing display (:99) and launches Chromium on it.
exec python -m computeruse.computer.daemon --driver x11 --display "$DISPLAY" \
  --host 0.0.0.0 --port "$DAEMON_PORT" \
  --app chromium --no-first-run --no-default-browser-check --disable-infobars \
  --disable-session-crashed-bubble --disable-features=TranslateUI --no-sandbox \
  --password-store=basic \
  --window-position=0,0 --window-size="${SCREEN_SIZE/x/,}" --start-maximized \
  --remote-debugging-port=9222 --user-data-dir="$PROFILE_DIR" "$START_URL"
