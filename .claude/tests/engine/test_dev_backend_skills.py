"""
tests/engine/test_dev_backend_skills.py — dev_backend 的 wikilink skill 注入测试

只测纯函数 `_load_task_skills(task_text)` 的契约：
- 无 wikilink → 返回 ("", [], [])
- 命中 backend skill stem → 展开
- 命中 backend skill 完整路径 → 展开
- 其他角色 skill (A?-/F?-/TL?-) → filter_skip，不展开
- 未解析的 backend skill wikilink → 不抛错，进 unresolved
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import wikilink as wl_mod
from engine.wikilink import invalidate_cache as invalidate_wl_cache
from dev_backend import main as dev_backend_main


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 vault + 同时 monkeypatch wikilink 与 dev_backend.main 的 VAULT_ROOT。"""
    monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(dev_backend_main, "VAULT_ROOT", tmp_path)
    invalidate_wl_cache()
    yield tmp_path
    invalidate_wl_cache()


def _write(p: Path, content: str = "placeholder\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_no_wikilink_returns_empty(tmp_vault: Path):
    block, loaded, unresolved = dev_backend_main._load_task_skills(
        "本任务实现用户注册接口，遵循 PRD 描述。"
    )
    assert block == ""
    assert loaded == []
    assert unresolved == []


def test_single_backend_skill_stem(tmp_vault: Path):
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B5-空集守卫.md",
        "# B5 空集守卫\n查询无结果时返回空数组而非 None。",
    )
    block, loaded, unresolved = dev_backend_main._load_task_skills(
        "处理空集时遵循 [[B5-空集守卫]]。"
    )
    assert "B5-空集守卫" in loaded
    assert unresolved == []
    assert "空集守卫" in block
    assert "[[B5-空集守卫]]" in block  # 段头保留原引用


def test_multiple_backend_skills(tmp_vault: Path):
    _write(tmp_vault / "20-知识/角色技能/se/后端工程师/B5-空集守卫.md", "B5 内容")
    _write(tmp_vault / "20-知识/角色技能/se/后端工程师/B7-FastAPI共享资源async.md", "B7 内容")
    block, loaded, _ = dev_backend_main._load_task_skills(
        "本任务用到 [[B5-空集守卫]] 和 [[B7-FastAPI共享资源async]]。"
    )
    assert set(loaded) == {"B5-空集守卫", "B7-FastAPI共享资源async"}
    assert "B5 内容" in block
    assert "B7 内容" in block


def test_full_path_wikilink(tmp_vault: Path):
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B6-静态资源路径锚定.md",
        "B6 内容",
    )
    block, loaded, _ = dev_backend_main._load_task_skills(
        "见 [[20-知识/角色技能/se/后端工程师/B6-静态资源路径锚定]]。"
    )
    assert loaded == ["20-知识/角色技能/se/后端工程师/B6-静态资源路径锚定"]
    assert "B6 内容" in block


def test_other_role_skills_filter_skipped(tmp_vault: Path):
    """A?-/F?-/TL?- 不应被 backend 展开。"""
    _write(tmp_vault / "20-知识/角色技能/se/架构师/A2-失败模式.md", "A2 内容")
    _write(tmp_vault / "20-知识/角色技能/se/前端工程师/F1-fetch响应检查.md", "F1 内容")
    _write(tmp_vault / "20-知识/角色技能/se/技术主管/TL1-任务完整性.md", "TL1 内容")
    _write(tmp_vault / "20-知识/角色技能/se/后端工程师/B5-空集守卫.md", "B5 内容")

    block, loaded, _ = dev_backend_main._load_task_skills(
        "用 [[B5-空集守卫]] 和 [[A2-失败模式]] 和 [[F1-fetch响应检查]] 和 [[TL1-任务完整性]]"
    )
    # 只有 B5 命中
    assert loaded == ["B5-空集守卫"]
    assert "B5 内容" in block
    assert "A2 内容" not in block
    assert "F1 内容" not in block
    assert "TL1 内容" not in block


def test_unresolved_backend_skill_warns_not_raises(tmp_vault: Path, capsys):
    """task 写错 wikilink 不阻断；记录到 unresolved，打 stderr warn。"""
    block, loaded, unresolved = dev_backend_main._load_task_skills(
        "处理空集时遵循 [[B99-不存在的skill]]。"
    )
    assert loaded == []
    assert unresolved == ["B99-不存在的skill"]
    assert block == ""
    assert "未解析" in capsys.readouterr().err


def test_duplicate_stem_does_not_crash_task(tmp_vault: Path, capsys):
    """命名规则被破坏（重名）时不应阻断 task，应打 warn 后返回空块。"""
    _write(tmp_vault / "20-知识/角色技能/se/后端工程师/B5-空集守卫.md", "原版")
    _write(tmp_vault / "99-备份/B5-空集守卫.md", "重名备份")  # 99-临时 不被排除，但 99-备份 在 stem 索引内 → 重名
    # 实际：99-备份/ 没在排除列表，会进 stem 索引
    block, loaded, unresolved = dev_backend_main._load_task_skills(
        "用 [[B5-空集守卫]]"
    )
    # 不抛错；block 空；stderr 有警告
    assert block == ""
    assert loaded == []
    captured = capsys.readouterr()
    assert "wikilink 展开失败" in captured.err


# ── keyword 触发器扩展测试（2026-05-22 P0 落地）──────────────────


def _skill_with_trigger(*, trigger: dict, body: str = "## 核心约束\n约束内容。\n") -> str:
    """构造带 trigger frontmatter 的 skill 文件内容。"""
    lines = ["type: skill", "role: 后端工程师", "trigger:"]
    for k, v in trigger.items():
        if isinstance(v, list):
            lines.append(f"  {k}:")
            for item in v:
                lines.append(f"    - {item!r}")
        else:
            lines.append(f"  {k}: {v}")
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body


def test_keyword_trigger_loads_when_task_has_keyword(tmp_vault: Path):
    """task_text 含 keyword 时 skill 自动触发（无需 task 写 wikilink）。"""
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B6-静态资源路径锚定.md",
        _skill_with_trigger(
            trigger={"keywords": ["StaticFiles"]},
            body="## 核心约束\n用 resolve_path 锚定静态目录。\n",
        ),
    )
    block, loaded, unresolved = dev_backend_main._load_task_skills(
        "本任务实现 FastAPI app 挂载 StaticFiles 到 /static 路径"
    )
    assert "B6-静态资源路径锚定" in loaded
    assert "用 resolve_path 锚定" in block
    assert "auto-trigger:keyword:StaticFiles" in block


def test_keyword_trigger_always_true_loads_for_any_task(tmp_vault: Path):
    """always=true 的 skill（如 B5 空集守卫）对任意 task 都注入。"""
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B5-空集守卫.md",
        _skill_with_trigger(
            trigger={"always": True},
            body="## 核心约束\nfetchone 必须先判 None。\n",
        ),
    )
    block, loaded, _ = dev_backend_main._load_task_skills(
        "实现无关的健康检查接口"
    )
    assert "B5-空集守卫" in loaded
    assert "fetchone 必须先判 None" in block
    assert "auto-trigger:always" in block


def test_wikilink_and_keyword_dedup(tmp_vault: Path):
    """wikilink 显式 + keyword 隐式命中同一 skill 时只注入一次。"""
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B5-空集守卫.md",
        _skill_with_trigger(
            trigger={"always": True},
            body="## 核心约束\nfetchone 必须先判 None。\n",
        ),
    )
    block, loaded, _ = dev_backend_main._load_task_skills(
        "处理空集时遵循 [[B5-空集守卫]]"
    )
    # wikilink 加载一次 + keyword 路径去重不再注入 → loaded 只含一份
    assert loaded.count("B5-空集守卫") == 1
    # 内容只出现一次（避免双倍 token 浪费）
    assert block.count("fetchone 必须先判 None") == 1


def test_skill_without_trigger_not_auto_loaded(tmp_vault: Path):
    """trigger 字段缺失的 skill 不会被 keyword 路径自动加载（fail-closed）。"""
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B_legacy.md",
        "---\ntype: skill\nrole: 后端工程师\n---\n\n## 核心约束\n旧 skill 无 trigger\n",
    )
    block, loaded, _ = dev_backend_main._load_task_skills(
        "任意 task 文本"
    )
    # fail-closed：trigger 缺失 → 不触发
    assert loaded == []
    assert block == ""


def test_keyword_trigger_with_upstream_text(tmp_vault: Path):
    """keyword 也匹配 upstream_text（如已读的技术栈 / PRD 内容）。"""
    _write(
        tmp_vault / "20-知识/角色技能/se/后端工程师/B7-FastAPI共享资源async.md",
        _skill_with_trigger(
            trigger={"keywords": ["FastAPI"]},
            body="## 核心约束\nasync def 中禁止裸 import requests。\n",
        ),
    )
    block, loaded, _ = dev_backend_main._load_task_skills(
        task_text="实现用户注册",
        upstream_text="技术栈：FastAPI 0.115 + SQLAlchemy",
    )
    assert "B7-FastAPI共享资源async" in loaded
    assert "async def 中禁止" in block
