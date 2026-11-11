from __future__ import annotations
import errno, os, time
from pathlib import Path
from typing import List, Tuple
from .errors import LockBusy


def _lock_backend() -> Tuple[str, object]:
    """``fcntl`` on POSIX. ``msvcrt`` where that module does not exist (Windows)."""
    try:
        import fcntl
        return "fcntl", fcntl
    except ImportError:
        pass
    try:
        import msvcrt
        return "msvcrt", msvcrt
    except ImportError:
        pass
    raise ImportError("file locking needs fcntl (POSIX) or msvcrt (Windows)")


def _is_lock_contention(exc: BaseException) -> bool:
    if isinstance(exc, (BlockingIOError, PermissionError)):
        return True
    err = getattr(exc, "errno", None)
    codes = {
        getattr(errno, name)
        for name in ("EACCES", "EAGAIN", "EDEADLK", "EWOULDBLOCK")
        if hasattr(errno, name)
    }
    return err in codes


def _prepare_msvcrt_region(fd: int) -> None:
    """``msvcrt.locking`` locks a byte range and fails on an empty file."""
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
    except OSError:
        size = 0
    if size < 1:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b" ")
        except OSError:
            pass
    os.lseek(fd, 0, os.SEEK_SET)


def lock_exclusive(fd: int, *, nonblocking: bool) -> None:
    """Exclusive lock. POSIX: ``flock``. Otherwise one byte at offset 0 via ``msvcrt``."""
    kind, mod = _lock_backend()
    if kind == "fcntl":
        flags = mod.LOCK_EX | (mod.LOCK_NB if nonblocking else 0)
        mod.flock(fd, flags)
        return
    _prepare_msvcrt_region(fd)
    mode = mod.LK_NBLCK if nonblocking else mod.LK_LOCK
    try:
        mod.locking(fd, mode, 1)
    except OSError as exc:
        if nonblocking and _is_lock_contention(exc):
            raise BlockingIOError(exc.errno or 0, "lock busy") from exc
        raise


def unlock(fd: int) -> None:
    kind, mod = _lock_backend()
    if kind == "fcntl":
        mod.flock(fd, mod.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    mod.locking(fd, mod.LK_UNLCK, 1)

def data_root(hermes_home: Path) -> Path:
    return hermes_home / "plugin-data" / "dream-cycle"

def is_disabled(root: Path, name: str = "DISABLED") -> bool:
    return (root / name).exists()

def acquire_lock(lock_path: Path, timeout_s: int = 5) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    start = time.time()
    while True:
        try:
            lock_exclusive(fd, nonblocking=True)
            return fd
        except BlockingIOError:
            if time.time() - start > timeout_s:
                os.close(fd)
                raise LockBusy(str(lock_path))
            time.sleep(0.05)
        except Exception:
            os.close(fd)
            raise

def release_lock(fd: int) -> None:
    try:
        unlock(fd)
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
    """Hold Hermes' own per-file lock: ``<file>.lock`` + an exclusive lock.

    Same convention as hermes-agent tools/memory_tool_store.py ``_file_lock``
    (``path.with_suffix(path.suffix + ".lock")``, O_RDWR|O_CREAT|O_NOFOLLOW,
    0600). POSIX takes ``fcntl.flock``. Windows has no ``fcntl``; the same
    lock file is locked with ``msvcrt.locking`` (one byte at offset 0), which
    is what the Hermes memory tool uses there, so the two writers still
    exclude each other. Non-blocking retries up to ``timeout_s``, then
    LockBusy (never hangs a cron).
    """
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
                lock_exclusive(fd, nonblocking=True)
                break
            except BlockingIOError:
                if time.time() - start > timeout_s:
                    raise MemoryLockBusy(str(lock_path))
                time.sleep(0.05)
        try:
            yield lock_path
        finally:
            try:
                unlock(fd)
            except OSError:
                pass
    finally:
        os.close(fd)
