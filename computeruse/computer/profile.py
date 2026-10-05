"""Chrome profile management for the browser backend.

A *persistent* profile keeps cookies, logins and SSO tokens between sessions, so
an operator signs in to an internal system once and the agent can use it in every
later session. Chrome allows a single running instance per profile directory, so
the shared profile is guarded by an advisory lock: the first browser session gets
the real profile, concurrent ones get a throwaway *copy* (existing logins carry
over, but nothing they do is written back). An *ephemeral* lease is a fresh,
empty profile that is deleted when the session ends (evals use these).

What the profile does *not* carry: client certificates and device-trust state
live in the OS user's NSS database (~/.pki/nssdb) and in system Chrome policies,
which Chrome reads regardless of the profile directory.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

ProfileMode = Literal["persistent", "ephemeral"]
MODES: tuple[str, ...] = ("persistent", "ephemeral")

LOCK_FILE = ".computeruse.lock"
# Chrome's own single-instance markers. Left behind by a killed Chrome they make the next launch
# try to hand its URL to a process that no longer exists (or, worse, to a reused pid).
SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")
# Regenerable data that is large and pointless to copy into a throwaway profile.
CACHE_DIR_NAMES = frozenset({
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "CacheStorage", "ScriptCache", "blob_storage", "BrowserMetrics", "Crashpad",
    "component_crx_cache", "optimization_guide_model_store", "Safe Browsing",
})


class ProfileError(RuntimeError):
    pass


@dataclass
class ProfileLease:
    """A profile directory handed to one Chrome process. Call `release()` when Chrome has exited."""

    path: str
    mode: ProfileMode
    persistent: bool  # True: Chrome writes straight into the shared profile
    source: str | None = None  # persistent profile a throwaway copy was seeded from
    note: str = ""
    _lock: IO[bytes] | None = field(default=None, repr=False)
    _delete_on_release: bool = False

    def describe(self) -> dict[str, Any]:
        return {"path": self.path, "mode": self.mode, "persistent": self.persistent, "source": self.source,
                "note": self.note}

    def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            try:
                os.utime(lock.fileno())  # "last used" marker for `status()`
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()
        if self._delete_on_release:
            self._delete_on_release = False
            shutil.rmtree(self.path, ignore_errors=True)


class BrowserProfile:
    """Hands out profile directories for browser sessions (see module docstring)."""

    def __init__(self, root: Path | str, default_mode: str = "persistent") -> None:
        self.root = Path(root).absolute()
        if default_mode not in MODES:
            raise ValueError(f"unknown profile mode {default_mode!r}; expected one of {MODES}")
        self.default_mode: ProfileMode = default_mode  # type: ignore[assignment]

    # -- leases --------------------------------------------------------------

    def acquire(self, mode: str | None = None, wait_s: float = 0.0) -> ProfileLease:
        """Lease a profile directory.

        `wait_s` bounds how long to wait for the shared profile when another session still holds
        it. Sessions are reported as finished a moment before their Chrome has fully exited, so a
        session started right after another one would otherwise find the lock taken and get a
        throwaway copy; a few seconds of patience turns that into the real profile.
        """
        mode = mode or self.default_mode
        if mode not in MODES:
            raise ProfileError(f"unknown profile mode {mode!r}; expected one of {MODES}")
        if mode == "ephemeral":
            path = tempfile.mkdtemp(prefix="computeruse-chrome-")
            return ProfileLease(path=path, mode="ephemeral", persistent=False, _delete_on_release=True,
                                note="fresh profile; cookies and logins are discarded when the session ends")
        self._ensure_root()
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            lock = self._try_lock()
            if lock is not None and self._foreign_chrome_pid() is None:
                self._remove_stale_singletons()
                os.utime(lock.fileno())
                return ProfileLease(path=str(self.root), mode="persistent", persistent=True, _lock=lock,
                                    note="persistent profile; cookies and logins are kept for later sessions")
            if lock is not None:  # our lock is free but some other Chrome is using the directory
                lock.close()
                break  # a hand-launched Chrome will not go away on its own schedule: do not wait
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        copy = tempfile.mkdtemp(prefix="computeruse-chrome-copy-")
        try:
            self._copy_profile(self.root, copy)
        except OSError as e:
            shutil.rmtree(copy, ignore_errors=True)
            raise ProfileError(f"cannot copy browser profile {self.root}: {e}") from e
        return ProfileLease(
            path=copy, mode="persistent", persistent=False, source=str(self.root), _delete_on_release=True,
            note="another browser session holds the persistent profile; using a throwaway copy of it "
                 "(existing logins work, new ones will not be saved)",
        )

    def status(self) -> dict[str, Any]:
        """Cheap summary for the UI / doctor: where the profile lives and whether it is in use."""
        exists = (self.root / "Default").is_dir() or (self.root / "Local State").exists()
        in_use = False
        if self.root.is_dir():
            lock = self._try_lock()
            if lock is None or self._foreign_chrome_pid() is not None:
                in_use = True
            if lock is not None:
                lock.close()
        last_used = None
        marker = self.root / LOCK_FILE
        if marker.exists():
            last_used = marker.stat().st_mtime
        return {"mode": self.default_mode, "path": str(self.root), "exists": exists, "in_use": in_use,
                "size_mb": round(_dir_size(self.root) / 1e6, 1) if exists else 0.0, "last_used": last_used}

    # -- internals ------------------------------------------------------------

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)  # the profile holds session cookies for internal systems
        except OSError:
            pass

    def _try_lock(self) -> IO[bytes] | None:
        """Exclusive advisory lock on the profile; None when another session holds it."""
        f = open(self.root / LOCK_FILE, "a+b")  # noqa: SIM115 - the handle *is* the lock
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return None
            raise
        return f

    def _foreign_chrome_pid(self) -> int | None:
        """Pid of a live Chrome using this profile outside our lock (e.g. launched by hand), else None."""
        link = self.root / "SingletonLock"
        try:
            target = os.readlink(link)  # "<hostname>-<pid>"
        except OSError:
            return None
        host, _, pid_s = target.rpartition("-")
        if not pid_s.isdigit() or (host and host != socket.gethostname()):
            return None
        pid = int(pid_s)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return pid
        return pid

    def _remove_stale_singletons(self) -> None:
        for name in SINGLETON_FILES:
            try:
                os.unlink(self.root / name)
            except OSError:
                pass

    @staticmethod
    def _copy_profile(src: Path, dst: str) -> None:
        def ignore(_dir: str, names: list[str]) -> set[str]:
            return {n for n in names if n in CACHE_DIR_NAMES or n in SINGLETON_FILES or n == LOCK_FILE}

        shutil.copytree(src, dst, ignore=ignore, symlinks=True, dirs_exist_ok=True)


def _dir_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in CACHE_DIR_NAMES]
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def profile_age_label(ts: float | None) -> str:
    if not ts:
        return "never used"
    delta = max(0.0, time.time() - ts)
    if delta < 90:
        return "used just now"
    if delta < 5400:
        return f"used {int(delta // 60)} min ago"
    if delta < 172800:
        return f"used {delta / 3600:.0f} h ago"
    return f"used {delta / 86400:.0f} days ago"
