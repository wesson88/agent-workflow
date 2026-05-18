# .claude/tests/engine/conftest.py — 把 .claude/ 与 .claude/skills/ 加入 sys.path，
# 使 `from engine import ...` 与 `from <skill> import ...` 都能用
import sys
from pathlib import Path

CLAUDE_ROOT = Path(__file__).resolve().parent.parent.parent   # .../.claude/
sys.path.insert(0, str(CLAUDE_ROOT))
sys.path.insert(0, str(CLAUDE_ROOT / "skills"))
