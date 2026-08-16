"""
test_tl_fallback_completeness.py — TL 单 call 兜底的产出完整性校验（S4）

守 `technical_lead._verify_task_index_completeness`。

## 背景（mini-ledger §3，2026-05-21）

Plan/Detail 拆分失败 → 回退单 call 兜底，一次调用产出「索引 + 全部任务卡」。
max_tokens 装不下时，LLM 会产出**完整的索引**但只写出**前几个任务卡**。

实战后果：`给前端-T04.md`（记录列表筛选+翻页）与 `给前端-T05.md`（月度汇总+饼图）
双双缺失，而流程判成功继续往下走，下游前端工程师照着不完整的指令集开工。

检测难点是没有 ground truth —— 单 call 模式下没人知道该有几个任务。
破解：索引是 LLM 自己的产出，它自己声明了任务清单，**拿它当自证的 ground truth**。

CLI 路径拿不到 API 的 stop_reason（engine/llm.py 从未透出该字段），
只能从产出形态反推。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from engine.config import PROJECT_ROOT


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location(
        "_tl_under_test", PROJECT_ROOT / ".claude" / "skills" / "technical_lead" / "main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._verify_task_index_completeness


def _index(*task_ids: str) -> str:
    rows = "\n".join(f"| {t} | 标题 | 3 | — |" for t in task_ids)
    return (
        "# 任务索引\n\n"
        f"> 共 {len(task_ids)} 个任务。\n\n"
        "| id | 标题 | 工时(h) | depends_on |\n"
        "|----|------|---------|------------|\n"
        f"{rows}\n"
    )


def _files(side: str, index_tasks, produced_tasks) -> dict:
    out = {f"10-项目/x/指令/给{side}-索引.md": _index(*index_tasks)}
    for t in produced_tasks:
        out[f"10-项目/x/指令/给{side}-{t}.md"] = f"# {t} 任务规格"
    return out


class TestMiniLedgerRegression:
    """锁住 2026-05-21 实战形态：索引 5 个任务，只产出前 3 个。"""

    def test_missing_tasks_detected(self, verify):
        files = _files("前端", ["T01", "T02", "T03", "T04", "T05"], ["T01", "T02", "T03"])
        declared, missing = verify(files, "前端")
        assert declared == ["T01", "T02", "T03", "T04", "T05"]
        assert missing == ["T04", "T05"]

    def test_complete_output_passes(self, verify):
        ids = ["T01", "T02", "T03", "T04", "T05"]
        declared, missing = verify(_files("前端", ids, ids), "前端")
        assert declared == ids and missing == []


class TestBoundaries:
    def test_no_index_no_false_alarm(self, verify):
        """没产出索引时无从比对 —— 不能瞎报，那会把正常路径卡死。"""
        assert verify({"10-项目/x/指令/给后端-T01.md": "x"}, "后端") == ([], [])

    def test_index_without_task_table(self, verify):
        """索引存在但没有任务表（纯散文）→ 无 ground truth，不报。"""
        files = {"10-项目/x/指令/给后端-索引.md": "# 索引\n\n本轮无子任务。\n"}
        assert verify(files, "后端") == ([], [])

    def test_lettered_task_id(self, verify):
        """TL 拆分会产出 T03a / T03b 这类带字母后缀的 id。"""
        files = _files("后端", ["T01", "T03a", "T03b"], ["T01", "T03a"])
        declared, missing = verify(files, "后端")
        assert declared == ["T01", "T03a", "T03b"]
        assert missing == ["T03b"]

    def test_side_isolation(self, verify):
        """前端索引不该被后端产出满足 —— 两侧独立校验。"""
        files = {
            "10-项目/x/指令/给前端-索引.md": _index("T01", "T02"),
            "10-项目/x/指令/给后端-T01.md": "x",
            "10-项目/x/指令/给后端-T02.md": "x",
        }
        _, missing = verify(files, "前端")
        assert missing == ["T01", "T02"]

    def test_extra_produced_tasks_are_not_an_error(self, verify):
        """产出多于索引声明 → 不是「缺失」，本校验只管少不管多。"""
        files = _files("后端", ["T01"], ["T01", "T02"])
        _, missing = verify(files, "后端")
        assert missing == []

    def test_windows_style_path_separator(self, verify):
        files = {
            "10-项目\\x\\指令\\给后端-索引.md": _index("T01", "T02"),
            "10-项目\\x\\指令\\给后端-T01.md": "x",
        }
        _, missing = verify(files, "后端")
        assert missing == ["T02"]
