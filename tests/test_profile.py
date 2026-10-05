"""Chrome profile leases: persistent lock + throwaway copies, ephemeral temp dirs, status."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from computeruse.computer.profile import (
    LOCK_FILE,
    BrowserProfile,
    ProfileError,
    profile_age_label,
)

COOKIES = b"c" * 2_000_000  # 2 MB of "logins" (counted)
CACHE = b"x" * 3_000_000  # 3 MB of regenerable cache (skipped by copies and size)


def seed_profile(root: Path) -> None:
    """A minimal Chrome-shaped profile: cookies to keep, caches to skip."""
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Cookies").write_bytes(COOKIES)
    (root / "Default" / "Cache").mkdir()
    (root / "Default" / "Cache" / "blob").write_bytes(CACHE)
    (root / "GrShaderCache").mkdir()
    (root / "GrShaderCache" / "data").write_bytes(b"y" * 4096)
    (root / "Local State").write_text("{}")


def test_ephemeral_lease_is_a_fresh_dir_deleted_on_release(tmp_path):
    prof = BrowserProfile(tmp_path / "profile")
    lease = prof.acquire("ephemeral")
    assert lease.mode == "ephemeral" and lease.persistent is False and lease.source is None
    assert Path(lease.path).is_dir() and Path(lease.path) != tmp_path / "profile"
    assert "discarded" in lease.note
    lease.release()
    assert not Path(lease.path).exists()
    lease.release()  # idempotent
    assert not (tmp_path / "profile").exists()  # the shared profile was never touched


def test_persistent_lease_locks_root_and_concurrent_sessions_get_copies(tmp_path):
    root = tmp_path / "profile"
    seed_profile(root)
    prof = BrowserProfile(root)
    assert prof.status()["in_use"] is False and prof.status()["exists"] is True

    first = prof.acquire()  # default mode is persistent
    assert first.persistent is True and first.mode == "persistent" and Path(first.path) == root
    assert (root / LOCK_FILE).exists()
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert prof.status()["in_use"] is True

    second = prof.acquire("persistent")
    assert second.persistent is False and second.mode == "persistent" and second.source == str(root)
    copy = Path(second.path)
    assert copy != root and copy.is_dir()
    assert (copy / "Default" / "Cookies").read_bytes() == COOKIES  # existing logins carry over
    assert not (copy / "Default" / "Cache").exists() and not (copy / "GrShaderCache").exists()
    assert not (copy / LOCK_FILE).exists()
    assert "throwaway copy" in second.note

    second.release()
    assert not copy.exists()
    assert prof.status()["in_use"] is True  # still held by `first`
    first.release()
    st = prof.status()
    assert st["in_use"] is False and st["last_used"] is not None and time.time() - st["last_used"] < 60
    assert st["path"] == str(root) and st["mode"] == "persistent"
    assert st["size_mb"] == 2.0  # cache dirs are not counted towards the size shown in the UI
    # Free again: the next persistent lease is the real thing.
    third = prof.acquire()
    assert third.persistent is True
    third.release()


def test_stale_singleton_files_are_removed_before_launch(tmp_path):
    root = tmp_path / "profile"
    seed_profile(root)
    os.symlink(f"{socket.gethostname()}-999999999", root / "SingletonLock")  # pid that cannot exist
    (root / "SingletonSocket").write_bytes(b"")
    prof = BrowserProfile(root)
    assert prof.status()["in_use"] is False
    lease = prof.acquire()
    assert lease.persistent is True
    assert not (root / "SingletonLock").is_symlink() and not (root / "SingletonSocket").exists()
    lease.release()


def test_hand_launched_chrome_on_the_profile_forces_a_copy(tmp_path):
    root = tmp_path / "profile"
    seed_profile(root)
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", root / "SingletonLock")  # a live pid: this test
    prof = BrowserProfile(root)
    assert prof.status()["in_use"] is True
    lease = prof.acquire()
    assert lease.persistent is False and lease.source == str(root)
    assert (root / "SingletonLock").is_symlink()  # never yank a running Chrome's lock
    lease.release()


def test_singleton_lock_from_another_host_is_ignored(tmp_path):
    root = tmp_path / "profile"
    seed_profile(root)
    os.symlink(f"other-host-{os.getpid()}", root / "SingletonLock")
    prof = BrowserProfile(root)
    assert prof.status()["in_use"] is False
    lease = prof.acquire()
    assert lease.persistent is True
    lease.release()


def test_invalid_modes_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown profile mode"):
        BrowserProfile(tmp_path, default_mode="bogus")
    prof = BrowserProfile(tmp_path / "p", default_mode="ephemeral")
    with pytest.raises(ProfileError, match="unknown profile mode"):
        prof.acquire("incognito")
    lease = prof.acquire()  # default mode is honoured
    assert lease.mode == "ephemeral"
    lease.release()
    assert prof.status()["mode"] == "ephemeral" and prof.status()["exists"] is False


def test_describe_and_age_label(tmp_path):
    prof = BrowserProfile(tmp_path / "p")
    lease = prof.acquire()
    d = lease.describe()
    assert set(d) == {"path", "mode", "persistent", "source", "note"} and d["persistent"] is True
    lease.release()
    now = time.time()
    assert profile_age_label(None) == "never used"
    assert profile_age_label(now - 10) == "used just now"
    assert profile_age_label(now - 600) == "used 10 min ago"
    assert profile_age_label(now - 3 * 3600) == "used 3 h ago"
    assert profile_age_label(now - 5 * 86400) == "used 5 days ago"


def test_acquire_waits_briefly_for_a_lock_that_is_about_to_be_released(tmp_path):
    import threading

    root = tmp_path / "profile"
    seed_profile(root)
    prof = BrowserProfile(root)
    first = prof.acquire()
    # Released while the second acquire is waiting (a session winding down): the real profile is handed over.
    threading.Timer(0.4, first.release).start()
    t0 = time.monotonic()
    second = prof.acquire(wait_s=5.0)
    assert second.persistent is True and 0.3 <= time.monotonic() - t0 < 4.0
    # Still held after the wait: fall back to a copy, without waiting forever.
    t0 = time.monotonic()
    third = prof.acquire(wait_s=0.5)
    assert third.persistent is False and third.source == str(root) and 0.4 <= time.monotonic() - t0 < 3.0
    third.release()
    second.release()
    # A hand-launched Chrome is not waited for at all.
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", root / "SingletonLock")
    t0 = time.monotonic()
    fourth = prof.acquire(wait_s=5.0)
    assert fourth.persistent is False and time.monotonic() - t0 < 1.0
    fourth.release()
