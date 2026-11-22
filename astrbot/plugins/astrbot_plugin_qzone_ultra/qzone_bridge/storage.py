"""Persistent storage helpers."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .models import BridgeState
from .utils import ensure_dir, now_iso


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.RLock()


def _thread_lock_for(path: Path) -> threading.RLock:
    key = path.resolve(strict=False)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


_LOCK_RETRY_SECONDS = 30.0


def _open_lock_file(path: Path, attempts: int = 20, delay: float = 0.05):
    """打开锁文件；被别的进程独占时退避重试，不把 Errno 13 抛到上面去。"""
    last_error: OSError | None = None
    for _ in range(attempts):
        try:
            return path.open("a+b")
        except OSError as exc:
            last_error = exc
            time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise OSError("cannot open lock file")


def _acquire_lock(lock_file) -> bool:
    """跨进程加锁；超时就放行，返回 False，绝不抛异常。"""
    if os.name != "nt":
        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return True
        except OSError:
            return False

    import msvcrt

    deadline = time.monotonic() + _LOCK_RETRY_SECONDS
    while True:
        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _release_lock(lock_file) -> None:
    try:
        if os.name != "nt":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


class StateStore:
    def __init__(self, root: Path):
        self.root = ensure_dir(root)
        self.path = self.root / "state.json"
        self.lock_path = self.root / "state.json.lock"
        self._thread_lock = _thread_lock_for(self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            try:
                lock_file = _open_lock_file(self.lock_path)
            except OSError:
                lock_file = None
            if lock_file is None:
                # 连锁文件都开不了，也别让整个进程陪葬
                yield
                return
            with lock_file:
                # 注意：不能去 read 这一字节，别人持锁时连读都会 PermissionError
                try:
                    if lock_file.seek(0, os.SEEK_END) == 0:
                        lock_file.write(b"\0")
                        lock_file.flush()
                except OSError:
                    pass
                lock_file.seek(0)
                locked = _acquire_lock(lock_file)
                try:
                    yield
                finally:
                    if locked:
                        _release_lock(lock_file)

    def read(self) -> BridgeState:
        with self._locked():
            return self._read_unlocked()

    def _read_unlocked(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            backup = self.root / f"state.corrupt.{secrets.token_hex(4)}.json"
            try:
                self.path.replace(backup)
                _chmod_private(backup)
            except Exception:
                pass
            return BridgeState()
        return BridgeState.from_dict(payload)

    def write(self, state: BridgeState) -> None:
        with self._locked():
            current = self._read_unlocked()
            if current.session.revision > state.session.revision:
                state.session = current.session
            self._write_unlocked(state)

    def _write_unlocked(self, state: BridgeState) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{secrets.token_hex(4)}")
        tmp.write_text(payload, encoding="utf-8")
        _chmod_private(tmp)
        tmp.replace(self.path)
        _chmod_private(self.path)

    def update(self, updater: Callable[[BridgeState], None]) -> BridgeState:
        with self._locked():
            state = self._read_unlocked()
            original_revision = state.session.revision
            updater(state)
            current = self._read_unlocked()
            if current.session.revision > original_revision and state.session.revision <= current.session.revision:
                state.session = current.session
            self._write_unlocked(state)
            return state


def ensure_state_secret(state: BridgeState) -> BridgeState:
    if not state.runtime.secret:
        state.runtime.secret = secrets.token_urlsafe(32)
        state.runtime.started_at = state.runtime.started_at or now_iso()
    return state
