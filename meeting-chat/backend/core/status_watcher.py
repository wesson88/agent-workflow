"""
监听 .claude/status.json 的变化并回调 on_change(snapshot)。

- 使用 watchdog 的 Observer 监视 .claude 目录（避免对单文件的平台兼容性问题）
- 200ms 去抖（status.json 用 write-temp-then-rename 原子写入，可能产生 create/modify 事件对）
- 启动时先做一次同步读取，把初始快照也回调一次
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Awaitable, Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

_BASE_DIR = Path(__file__).parent.parent
CLAUDE_ROOT = (_BASE_DIR / ".." / ".." / ".claude").resolve()
STATUS_PATH = CLAUDE_ROOT / "status.json"

DEBOUNCE_SECONDS = 0.2

ChangeCallback = Callable[[dict], Awaitable[None]]


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "StatusWatcher"):
        self._watcher = watcher

    def _is_target(self, event: FileSystemEvent) -> bool:
        try:
            return Path(event.src_path).resolve() == STATUS_PATH
        except Exception:
            return False

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_target(event):
            self._watcher._schedule_reload()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._is_target(event):
            self._watcher._schedule_reload()

    def on_moved(self, event: FileSystemEvent) -> None:
        # 原子写入通常表现为 create temp + move temp -> target
        try:
            if Path(getattr(event, "dest_path", "")).resolve() == STATUS_PATH:
                self._watcher._schedule_reload()
        except Exception:
            pass


class StatusWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, on_change: ChangeCallback):
        self._loop = loop
        self._on_change = on_change
        self._observer: Optional[Observer] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not CLAUDE_ROOT.exists():
            return
        observer = Observer()
        handler = _Handler(self)
        observer.schedule(handler, str(CLAUDE_ROOT), recursive=False)
        observer.start()
        self._observer = observer
        self._emit_snapshot()

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=1.0)
            except Exception:
                pass
            self._observer = None

    def _schedule_reload(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._emit_snapshot)
            self._timer.daemon = True
            self._timer.start()

    def _emit_snapshot(self) -> None:
        snapshot = self._read_status()
        if snapshot is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._on_change(snapshot), self._loop)
        except Exception:
            pass

    @staticmethod
    def _read_status() -> Optional[dict]:
        if not STATUS_PATH.exists():
            return None
        try:
            with STATUS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # 原子写入过程中短暂的不完整读，忽略；下一次事件会兜底
            return None

    @staticmethod
    def read_once() -> Optional[dict]:
        """供外部（如 HTTP 端点首次加载）同步读取一次。"""
        return StatusWatcher._read_status()
