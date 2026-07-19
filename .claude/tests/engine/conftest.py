# .claude/tests/engine/conftest.py — 把 .claude/ 与 .claude/skills/ 加入 sys.path，
# 使 `from engine import ...` 与 `from <skill> import ...` 都能用
import sys
from pathlib import Path

CLAUDE_ROOT = Path(__file__).resolve().parent.parent.parent   # .../.claude/
sys.path.insert(0, str(CLAUDE_ROOT))
sys.path.insert(0, str(CLAUDE_ROOT / "skills"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _artifact_check_off(monkeypatch):
    """v0.3 产物校验默认 warn 会在测试里读真实 vault + 写真实 audit.jsonl；
    测试全局关闭，需要校验行为的用例自行 setenv 覆盖。"""
    monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "off")
