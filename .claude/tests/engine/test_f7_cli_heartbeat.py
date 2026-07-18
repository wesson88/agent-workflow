"""
test_f7_cli_heartbeat.py — F7 阶段 A：CLI 心跳/僵尸/硬超时机制（G1 验收）

设计：[[F7-invoke_role-联合设计-2026-07-18]] §3
- M1 心跳：heartbeat_s 无 stdout → kill 进程树 + CliHeartbeatTimeout
- M3 僵尸：proc 已 exit 但 EOF 未达 → 连续 2 个 poll 周期后正常收尾
- 硬超时：总时长超限（无论输出是否流动）→ CliHardTimeout
- 遥测：stats.max_gap_s 记录最大 stdout 间隔（G4 校准数据）
- usage：stream-json result 事件的 usage 提取（CLI telemetry 断点接通）

阈值全部用小值注入（生成器参数），单测秒级完成。
真实子进程集成测试用 sys.executable 起一个 sleep 脚本验证 kill 生效。
"""

from __future__ import annotations

import queue
import subprocess
import sys
import time

import pytest

from engine.llm import (
    CliHardTimeout,
    CliHeartbeatTimeout,
    _QUEUE_EOF,
    _iter_lines_with_heartbeat,
    _read_stream_json,
)


class _FakeProc:
    """poll/pid 可控的假 Popen；kill 计数。"""

    def __init__(self, poll_result=None):
        self.pid = 99999
        self._poll_result = poll_result
        self.killed = False

    def poll(self):
        return self._poll_result

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self._poll_result


@pytest.fixture
def no_real_kill(monkeypatch):
    """单元层不真调 taskkill；记录调用。"""
    from engine import llm as llm_mod
    killed = []
    monkeypatch.setattr(llm_mod, "_kill_process_tree", lambda p: killed.append(p))
    return killed


class TestHeartbeat:
    def test_no_output_triggers_heartbeat_kill(self, no_real_kill):
        """G1 核心：无输出超过 heartbeat_s → kill + CliHeartbeatTimeout。"""
        q: queue.Queue = queue.Queue()   # 永远空
        proc = _FakeProc(poll_result=None)  # 进程"活着"
        gen = _iter_lines_with_heartbeat(
            proc, q, {}, heartbeat_s=0.3, hard_timeout_s=60, poll_s=0.05,
        )
        with pytest.raises(CliHeartbeatTimeout):
            list(gen)
        assert no_real_kill == [proc]

    def test_output_flow_no_false_kill(self, no_real_kill):
        """有输出流动时不误杀；EOF 正常收尾；max_gap 被记录。"""
        q: queue.Queue = queue.Queue()
        now = time.monotonic()
        q.put((now, b"line1\n"))
        q.put((now + 0.1, b"line2\n"))
        q.put(_QUEUE_EOF)
        stats: dict = {}
        proc = _FakeProc()
        lines = list(_iter_lines_with_heartbeat(
            proc, q, stats, heartbeat_s=5, hard_timeout_s=60, poll_s=0.05,
        ))
        assert lines == [b"line1\n", b"line2\n"]
        assert no_real_kill == []
        assert stats["max_gap_s"] >= 0.0

    def test_hard_timeout_even_with_output(self, no_real_kill):
        """输出仍在流动但总时长超硬上限 → CliHardTimeout（总墙钟语义）。"""
        q: queue.Queue = queue.Queue()
        proc = _FakeProc()

        def feed():
            # 持续喂行直到被 hard timeout 打断
            for _ in range(200):
                q.put((time.monotonic(), b"x\n"))
                time.sleep(0.01)

        import threading
        threading.Thread(target=feed, daemon=True).start()
        gen = _iter_lines_with_heartbeat(
            proc, q, {}, heartbeat_s=60, hard_timeout_s=0.3, poll_s=0.05,
        )
        with pytest.raises(CliHardTimeout):
            list(gen)
        assert no_real_kill == [proc]

    def test_zombie_exit_breaks_cleanly(self, no_real_kill):
        """M3：proc 已 exit + 无 EOF 哨兵 → 连续 2 个 poll 后正常收尾（不 raise）。"""
        q: queue.Queue = queue.Queue()   # 永远空、也无 EOF（R3 现象模拟）
        proc = _FakeProc(poll_result=0)  # 已退出
        lines = list(_iter_lines_with_heartbeat(
            proc, q, {}, heartbeat_s=60, hard_timeout_s=60, poll_s=0.05,
        ))
        assert lines == []
        assert no_real_kill == []   # 僵尸收尾不 kill（进程已死）


class TestRealSubprocessKill:
    def test_hang_subprocess_killed_for_real(self, tmp_path):
        """集成：真实 python 子进程 sleep 假 hang → 心跳 kill 进程树生效。"""
        from engine import llm as llm_mod

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        q: queue.Queue = queue.Queue()  # 子进程无输出 → 队列永远空
        gen = llm_mod._iter_lines_with_heartbeat(
            proc, q, {}, heartbeat_s=0.5, hard_timeout_s=60, poll_s=0.1,
        )
        t0 = time.monotonic()
        with pytest.raises(CliHeartbeatTimeout):
            list(gen)
        elapsed = time.monotonic() - t0
        assert elapsed < 30, "kill 后应快速返回而非等 sleep 结束"
        assert proc.poll() is not None, "子进程应已被杀死"


class TestCallCliEndToEnd:
    def test_fake_cli_full_pipeline_with_audit(self, tmp_path, monkeypatch):
        """端到端接线：假 CLI（python 脚本吐 stream-json）走完整 _call_cli
        （writer/reader 线程 + 心跳迭代器 + 解析 + llm_call 审计落盘）。"""
        import json as json_mod
        from engine import llm as llm_mod

        fake_cli = tmp_path / "fake_cli.py"
        fake_cli.write_text(
            "import sys\n"
            "sys.stdin.read()\n"
            'print(\'{"type": "assistant", "message": {"content": '
            '[{"type": "text", "text": "PONG"}]}}\')\n'
            'print(\'{"type": "result", "result": "PONG", '
            '"usage": {"input_tokens": 7, "output_tokens": 3}}\')\n',
            encoding="utf-8",
        )
        audit_path = tmp_path / "audit.jsonl"
        monkeypatch.setattr(llm_mod, "_AUDIT_JSONL_PATH", audit_path)

        cli_cfg = {
            "path": sys.executable,
            "extra_args": [str(fake_cli)],
            "output_format": "stream-json",
            "use_system_prompt_flag": False,
        }
        out = llm_mod._call_cli(
            cli_cfg, "system", "ping", print_stream=False,
            role_name="测试角色", model_name="fake-model",
        )
        assert out == "PONG"

        events = [
            json_mod.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        llm_calls = [e for e in events if e.get("reason") == "llm_call"]
        assert len(llm_calls) == 1
        ev = llm_calls[0]
        assert ev["track"] == "cli"
        assert ev["role"] == "测试角色"
        assert ev["input_tokens"] == 7
        assert ev["output_tokens"] == 3
        assert "max_stdout_gap_s" in ev


class TestStreamJsonUsage:
    def test_result_event_usage_extracted(self):
        lines = [
            b'{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}\n',
            b'{"type": "result", "result": "hi", "usage": {"input_tokens": 120, "output_tokens": 45, "cache_read_input_tokens": 80}}\n',
        ]
        chunks, usage = _read_stream_json(iter(lines), print_stream=False)
        assert chunks == ["hi"]
        assert usage["input_tokens"] == 120
        assert usage["output_tokens"] == 45
        assert usage["cache_read_input_tokens"] == 80

    def test_no_usage_graceful(self):
        lines = [b'{"type": "result", "result": "ok"}\n']
        chunks, usage = _read_stream_json(iter(lines), print_stream=False)
        assert chunks == ["ok"]
        assert usage == {}
