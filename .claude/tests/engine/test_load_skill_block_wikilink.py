"""
test_load_skill_block_wikilink.py — load_skill_block wikilink 显式路径回归锁定

背景（2026-07-18 huashu-demo 回归跑暴露）：load_skill_block 给 expand_wikilinks
传 filter=None（filter 是必填回调）→ TypeError 被宽 except 静默吞掉降级，
SE 域 wikilink 显式 skill 路径自上线起从未生效、一直只走 keyword 兜底。
修复 commit 658173d。本文件锁定该分支不再静默失效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))


_SKILL_MD = """\
---
skill_id: F9-测试专用
trigger:
  keywords: ["绝不匹配的触发词xyzzy"]
---
# F9-测试专用

## 核心约束

- 测试用核心约束内容 MARKER_F9
"""


@pytest.fixture
def skill_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """临时 vault：20-知识/角色技能/se/前端工程师/F9-测试专用.md"""
    import engine
    from engine import wikilink as wl_mod

    skill_dir = tmp_path / "20-知识" / "角色技能" / "se" / "前端工程师"
    skill_dir.mkdir(parents=True)
    (skill_dir / "F9-测试专用.md").write_text(_SKILL_MD, encoding="utf-8")

    monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
    wl_mod.invalidate_cache()
    yield tmp_path
    wl_mod.invalidate_cache()


class TestLoadSkillBlockWikilinkBranch:
    def test_explicit_wikilink_hit(self, skill_vault):
        """task 里的显式 [[skill]] 引用必须走 wikilink 路径加载（修复前恒 0 命中）。"""
        from common import load_skill_block

        block, hint = load_skill_block(
            "前端工程师", "实现列表页，注意 [[F9-测试专用]]", "",
        )
        assert "wikilink=1" in hint, f"wikilink 路径未命中：{hint}"
        assert "MARKER_F9" in block
        assert "核心约束" in block

    def test_no_wikilink_no_keyword_empty(self, skill_vault):
        from common import load_skill_block

        block, hint = load_skill_block("前端工程师", "普通任务无引用", "")
        assert block == ""
        assert "wikilink=0" in hint or "均空" in hint or "keyword=0" in hint

    def test_wikilink_branch_must_not_silently_degrade(self, skill_vault, capsys):
        """哨兵：wikilink 分支不得再出现"展开失败…仅走 keyword 路径"的静默降级。"""
        from common import load_skill_block

        load_skill_block("前端工程师", "check [[F9-测试专用]]", "")
        err = capsys.readouterr().err
        assert "wikilink 展开失败" not in err, f"wikilink 分支又静默降级了：{err}"
