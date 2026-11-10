from __future__ import annotations
import os, time
from pathlib import Path
from typing import List
from .errors import LockBusy

def data_root(hermes_home: Path) -> Path:
    return hermes_home / "plugin-data" / "dream-cycle"

def is_disabled(root: Path, name: str = "DISABLED") -> bool:
    return (root / name).exists()

def acquire_lock(lock_path: Path, timeout_s: int = 5) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    import fcntl
    start = time.time()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.time() - start > timeout_s:
                os.close(fd)
                raise LockBusy(str(lock_path))
            time.sleep(0.05)

def release_lock(fd: int) -> None:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

def resolve_memory_files(search_roots: List[Path], globs: List[str], shallow_roots: List[Path] = ()) -> List[Path]:
    """rglob each search root; shallow_roots get top-level files only.

    HERMES_HOME is a shallow root: recursing into it would pick up
    plugin-data/ snapshots, plugins/ test fixtures and skills/ (2026-11-08).
    """
    out: List[tuple] = []
    shallow = {Path(r).resolve() for r in shallow_roots}
    for root in search_roots:
        if not root.exists():
            continue
        walk = root.glob if Path(root).resolve() in shallow else root.rglob
        for g in globs:
            out.extend((root, p) for p in sorted(walk(g)) if p.is_file())
    seen = set(); uniq = []
    for root, p in out:
        rp = p.resolve()
        # Skip hidden dirs/files *below* the search root only. HERMES_HOME itself
        # normally lives under ~/.hermes, so checking every path part skipped
        # everything (2026-11-08 fix: scanned_files was always 0 in production).
        rel = p.relative_to(root).parts
        if rp in seen or any(part.startswith(".") for part in rel):
            continue
        seen.add(rp); uniq.append(p)
    return uniq


class MemoryLockBusy(LockBusy):
    pass


from contextlib import contextmanager


@contextmanager
def memory_file_lock(path: Path, timeout_s: float = 10.0):
    """Hold Hermes' own per-file lock: ``<file>.lock`` + exclusive flock.

    Same convention as hermes-agent tools/memory_tool_store.py ``_file_lock``
    (``path.with_suffix(path.suffix + ".lock")``, O_RDWR|O_CREAT|O_NOFOLLOW,
    0600, fcntl.flock LOCK_EX), so the Hermes memory tool and Dream Cycle
    apply/rollback never interleave a read-modify-write on MEMORY.md.
    Non-blocking retries up to ``timeout_s``, then LockBusy (never hangs a cron).
    """
    import fcntl
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        start = time.time()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() - start > timeout_s:
                    raise MemoryLockBusy(str(lock_path))
                time.sleep(0.05)
        try:
            yield lock_path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)
