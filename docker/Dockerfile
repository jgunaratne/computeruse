# Sandbox VM image: a disposable Linux desktop (Xvfb + Chromium + optional
# noVNC live view) that exposes the computeruse control daemon on :8800.
#
#   docker build -t computeruse-sandbox:latest -f docker/Dockerfile .
#   docker run --rm -p 8800:8800 -p 6080:6080 -e COMPUTERUSE_DAEMON_TOKEN=secret computeruse-sandbox
#
# The host uses it through the `docker` backend (spawns a container per
# session) or the `remote` backend (COMPUTERUSE_REMOTE_DAEMON_URL=http://host:8800).
#
# NOTE: this image has not been built/run on the development machine (no
# Docker there); it is the intended deployment shape and follows the same
# daemon contract that the `browser` backend exercises locally every run.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify \
      chromium fonts-liberation fonts-noto-color-emoji fonts-dejavu-core \
      libnss3 libatk-bridge2.0-0 libgtk-3-0 libxss1 libasound2 libgbm1 \
      xdotool procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pillow python-xlib

# Only the in-VM half of the package is needed inside the sandbox.
WORKDIR /opt/computeruse
COPY computeruse/__init__.py computeruse/__init__.py
COPY computeruse/computer/__init__.py computeruse/computer/__init__.py
COPY computeruse/computer/keys.py computeruse/computer/keys.py
COPY computeruse/computer/daemon.py computeruse/computer/daemon.py
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd -m -u 1000 agent && chown -R agent:agent /opt/computeruse
USER agent
ENV PYTHONPATH=/opt/computeruse \
    SCREEN_SIZE=1280x800 \
    DAEMON_PORT=8800 \
    NOVNC_PORT=6080 \
    START_URL=about:blank

EXPOSE 8800 6080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD curl -fs -H "Authorization: Bearer ${COMPUTERUSE_DAEMON_TOKEN:-}" http://127.0.0.1:${DAEMON_PORT}/health || exit 1
ENTRYPOINT ["/entrypoint.sh"]
