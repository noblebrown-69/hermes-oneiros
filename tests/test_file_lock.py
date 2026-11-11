"""POSIX keeps flock. A host without fcntl (Windows) uses msvcrt.locking.

The msvcrt branch is exercised with a stub. This environment has fcntl and
does not have a Windows kernel, so the stub records the calls the real
``msvcrt.locking`` would receive: one byte at offset 0, non-blocking or blocking.
"""
from __future__ import annotations

import builtins
import errno
import os
import sys

import pytest

from dream_cycle.errors import LockBusy
from dream_cycle.paths import (
    MemoryLockBusy,
    acquire_lock,
    lock_exclusive,
    memory_file_lock,
    release_lock,
    unlock,
)


class _Msvcrt:
    def __init__(self):
        self.LK_NBLCK = 1
        self.LK_LOCK = 2
        self.LK_UNLCK = 3
        self.calls = []
        self.busy = False

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if self.busy and mode == self.LK_NBLCK:
            raise PermissionError(errno.EACCES, "busy")


@pytest.fixture
def no_fcntl(monkeypatch):
    real_import = builtins.__import__
    fake = _Msvcrt()

    def guarded(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named fcntl")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "fcntl", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    return fake


def test_posix_lock_does_not_write_a_byte(tmp_path):
    path = tmp_path / "dream-cycle.lock"
    fd = acquire_lock(path, timeout_s=1)
    try:
        # flock does not need a byte in the file. A write here would mean
        # the Windows branch ran on this POSIX host.
        assert path.stat().st_size == 0
    finally:
        release_lock(fd)


def test_missing_fcntl_locks_one_byte_with_msvcrt(tmp_path, no_fcntl):
    path = tmp_path / "dream-cycle.lock"
    fd = acquire_lock(path, timeout_s=1)
    try:
        assert path.stat().st_size >= 1
        assert (no_fcntl.LK_NBLCK, 1) in no_fcntl.calls
    finally:
        release_lock(fd)
    assert (no_fcntl.LK_UNLCK, 1) in no_fcntl.calls


def test_missing_fcntl_contention_is_lock_busy(tmp_path, no_fcntl):
    no_fcntl.busy = True
    with pytest.raises(LockBusy):
        acquire_lock(tmp_path / "busy.lock", timeout_s=-1)


def test_memory_lock_contention_without_fcntl(tmp_path, no_fcntl):
    no_fcntl.busy = True
    target = tmp_path / "MEMORY.md"
    target.write_text("x\n")
    with pytest.raises(MemoryLockBusy):
        with memory_file_lock(target, timeout_s=-1):
            pass


def test_blocking_lock_uses_lk_lock(tmp_path, no_fcntl):
    path = tmp_path / "review-reminder.lock"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        lock_exclusive(fd, nonblocking=False)
        assert (no_fcntl.LK_LOCK, 1) in no_fcntl.calls
        unlock(fd)
        assert (no_fcntl.LK_UNLCK, 1) in no_fcntl.calls
    finally:
        os.close(fd)


def test_remind_lock_without_fcntl(tmp_path, no_fcntl):
    from dream_cycle.remind import _lock, _unlock
    fh = _lock(tmp_path)
    try:
        assert (no_fcntl.LK_LOCK, 1) in no_fcntl.calls
    finally:
        _unlock(fh)
    assert (no_fcntl.LK_UNLCK, 1) in no_fcntl.calls


def test_neither_lock_backend_closes_and_raises(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name in ("fcntl", "msvcrt"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "fcntl", raising=False)
    monkeypatch.delitem(sys.modules, "msvcrt", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ImportError, match="fcntl"):
        acquire_lock(tmp_path / "x.lock", timeout_s=0)
