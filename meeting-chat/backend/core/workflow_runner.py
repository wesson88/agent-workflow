"""
工作流子进程运行器 - 单例，一次只跑一个工作流任务。

触发来源：POST /api/workflow/run
- mode="all"   -> python .claude/script/optimize_all.py
- mode="skill" -> python .claude/script/workflow.py (TARGET_SKILL=<skill>)

stdout/stderr 合并后逐行回调 on_line；退出时回调 on_exit(returncode, task_id)。
已有任务在跑时 start() 返回 {"ok": False, "reason": "busy", ...}。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

_BASE_DIR = Path(__file__).parent.parent
CLAUDE_ROOT = (_BASE_DIR / ".." / ".." / ".claude").resolve()

VALID_SKILLS = {"chief_architect", "technical_lead", "dev_backend", "dev_frontend"}

LineCallback = Callable[[str, str, Optional[str], str], Awaitable[None]]
ExitCallback = Callable[[int, str, Literal["all", "skill"], Optional[str], str, str], Awaitable[None]]


class WorkflowRunner:
    def __init__(self, on_line: LineCallback, on_exit: ExitCallback):
        self._on_line = on_line
        self._on_exit = on_exit
        self._proc: Optional[subprocess.Popen] = None
        self._task_id: Optional[str] = None
        self._mode: Optional[Literal["all", "skill"]] = None
        self._skill: Optional[str] = None
        self._task_desc: Optional[str] = None
        self._meeting_id: Optional[str] = None
        self._started_at: Optional[float] = None
        self._reader_task: Optional[asyncio.Task] = None

    # ── 查询 ──────────────────────────────────────────────
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def current(self) -> Optional[dict]:
        if not self.is_running():
            return None
        return {
            "task_id":    self._task_id,
            "mode":       self._mode,
            "skill":      self._skill,
            "task":       self._task_desc,
            "meeting_id": self._meeting_id,
            "started_at": self._started_at,
            "pid":        self._proc.pid if self._proc else None,
        }

    # ── 启动 ──────────────────────────────────────────────
    def start(
        self,
        mode: Literal["all", "skill"],
        task: str,
        meeting_id: str,
        skill: Optional[str] = None,
    ) -> dict:
        if self.is_running():
            return {
                "ok":      False,
                "reason":  "busy",
                "current": self.current(),
            }

        if mode == "skill":
            if not skill or skill not in VALID_SKILLS:
                return {
                    "ok":     False,
                    "reason": "invalid_skill",
                    "detail": f"skill must be one of {sorted(VALID_SKILLS)}",
                }
            script_rel = "script/workflow.py"
        else:
            script_rel = "script/optimize_all.py"

        if not CLAUDE_ROOT.exists():
            return {
                "ok":     False,
                "reason": "claude_root_missing",
                "detail": f"{CLAUDE_ROOT} not found",
            }

        task_id = uuid.uuid4().hex[:10]
        env = os.environ.copy()
        env["TASK"] = task
        if mode == "skill":
            env["TARGET_SKILL"] = skill or ""
        env["WORKFLOW_TASK_ID"] = task_id

        try:
            proc = subprocess.Popen(
                ["python", "-u", script_rel],
                cwd=str(CLAUDE_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            return {"ok": False, "reason": "spawn_failed", "detail": str(exc)}

        self._proc       = proc
        self._task_id    = task_id
        self._mode       = mode
        self._skill      = skill
        self._task_desc  = task
        self._meeting_id = meeting_id
        self._started_at = time.time()
        self._reader_task = asyncio.create_task(self._read_loop())

        return {
            "ok":         True,
            "task_id":    task_id,
            "mode":       mode,
            "skill":      skill,
            "task":       task,
            "meeting_id": meeting_id,
            "started_at": self._started_at,
            "pid":        proc.pid,
        }

    # ── stdout 读取循环 ────────────────────────────────────
    async def _read_loop(self) -> None:
        proc = self._proc
        task_id = self._task_id or ""
        skill = self._skill
        meeting_id = self._meeting_id or ""
        assert proc is not None and proc.stdout is not None

        while True:
            line = await asyncio.to_thread(proc.stdout.readline)
            if not line:
                break
            try:
                await self._on_line(line.rstrip("\n"), task_id, skill, meeting_id)
            except Exception:
                pass

        rc = await asyncio.to_thread(proc.wait)
        mode = self._mode or "all"
        task_desc = self._task_desc or ""
        # 在回调前清除当前句柄，允许下一轮启动
        self._proc = None
        self._task_id = None
        self._mode = None
        self._skill = None
        self._task_desc = None
        self._meeting_id = None
        self._started_at = None
        self._reader_task = None
        try:
            await self._on_exit(rc, task_id, mode, skill, task_desc, meeting_id)
        except Exception:
            pass

    # ── 终止（B 阶段会用；当前 shutdown 也用到） ─────────────
    def terminate_if_running(self) -> None:
        if not self.is_running() or self._proc is None:
            return
        try:
            self._proc.terminate()
        except Exception:
            pass
