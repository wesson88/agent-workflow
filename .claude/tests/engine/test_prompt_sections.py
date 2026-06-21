"""
test_prompt_sections.py — skills/common.py _extract_role_prompt_sections 单元测试 (T2.7)

覆盖：
- business_strict 路径：§1-§6 严格抽取（含子节）+ §7+ 排除
- meta_full 路径：全 body 减 DYNAMIC + 版本历史
- 缺章 / 序号乱序 → RuntimeError
- 4 个改造后业务角色实测
- 3 个改造后创意角色实测
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude"))


# ── 1. business_strict 标准 ─────────────────────────────
def test_business_strict_parse_ok():
    from common import _extract_role_prompt_sections

    body = """# 角色：测试业务

## 1. 核心使命
A

## 2. 输入与输出
B

## 3. 职责范围
C

## 4. 边界
D

## 5. 工作流
E

## 6. 质量原则
F

## 7. 运行时补丁（控制区）
<!-- DYNAMIC_START -->
patch
<!-- DYNAMIC_END -->

## 8. 版本历史
v1
"""
    text, path = _extract_role_prompt_sections(body, "技术开发")
    assert path == "business_strict"
    assert "# 角色：测试业务" in text
    for kw in ["## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."]:
        assert kw in text, f"missing {kw}"
    for kw in ["## 7.", "## 8.", "DYNAMIC", "v1"]:
        assert kw not in text, f"should not contain {kw}"


# ── 2. business_strict 子节包含 ────────────────────────
def test_business_subsections_included():
    from common import _extract_role_prompt_sections

    body = """# 角色：x

## 1. A
## 2. B
## 3. C
### 3.1 子节
sub-3.1
### 3.2 子节
sub-3.2
## 4. D
## 5. E
## 6. F
### 6.1 质量子节
sub-6.1
## 7. DYNAMIC
"""
    text, path = _extract_role_prompt_sections(body, "技术开发")
    assert path == "business_strict"
    assert "sub-3.1" in text
    assert "sub-3.2" in text
    assert "sub-6.1" in text
    assert "## 3.1" in text
    assert "## 6.1" in text


# ── 3. business_strict §7+ 排除 ─────────────────────────
def test_business_section_7_excluded():
    from common import _extract_role_prompt_sections

    body = """# 角色：x

## 1. A
## 2. B
## 3. C
## 4. D
## 5. E
## 6. F
## 7. 运行时补丁
patch-content-should-not-be-included
## 8. 版本历史
version-content-should-not-be-included
"""
    text, _ = _extract_role_prompt_sections(body, "技术开发")
    assert "patch-content-should-not-be-included" not in text
    assert "version-content-should-not-be-included" not in text


# ── 4. business 缺章 raise ──────────────────────────────
def test_business_missing_section_raises():
    from common import _extract_role_prompt_sections

    body = """# 角色：x

## 1. A
## 2. B
## 3. C
## 4. D
## 6. F
"""
    with pytest.raises(RuntimeError, match="§1-§6 缺章"):
        _extract_role_prompt_sections(body, "技术开发")


# ── 5. business 无任何 ## 标题 raise ────────────────────
def test_business_no_sections_raises():
    from common import _extract_role_prompt_sections

    body = """# 角色：x

just plain text without any ## headings
"""
    with pytest.raises(RuntimeError, match="未找到任何"):
        _extract_role_prompt_sections(body, "技术开发")


# ── 6. meta_full 全 body 减 DYNAMIC + 版本历史 ─────────
def test_meta_full_extraction():
    from common import _extract_role_prompt_sections

    body = """# 角色：复盘者

## 1. 核心使命
A

## 2. 跨域适配机制
META-2

## 3. 输出格式
META-3

## 4. 复盘报告结构
META-4

## 9. 运行时补丁（控制区）
<!-- DYNAMIC_START -->
patch-content
<!-- DYNAMIC_END -->

## 10. 版本历史
v1-content
"""
    text, path = _extract_role_prompt_sections(body, "元")
    assert path == "meta_full"
    assert "META-2" in text
    assert "META-3" in text
    assert "META-4" in text


# ── 7. meta DYNAMIC marker 剥除 ─────────────────────────
def test_meta_dynamic_marker_stripped():
    from common import _extract_role_prompt_sections

    body = """# 角色：x

## 1. A
A

## 2. B
B

## 7. 运行时补丁（控制区）
<!-- DYNAMIC_START -->
secret-patch
<!-- DYNAMIC_END -->
"""
    text, _ = _extract_role_prompt_sections(body, "元")
    assert "secret-patch" not in text
    assert "DYNAMIC_START" not in text


# ── 8. meta 版本历史剥除 ────────────────────────────────
def test_meta_version_history_stripped():
    from common import _extract_role_prompt_sections

    body = """# 角色：x

## 1. A
A

## 8. 版本历史
v1-changelog-secret
"""
    text, _ = _extract_role_prompt_sections(body, "元")
    assert "v1-changelog-secret" not in text
    assert "版本历史" not in text


# ── 9. 4 个改造后业务角色实测 ──────────────────────────
@pytest.mark.parametrize("role_name", ["后端工程师", "前端工程师", "技术主管", "产品经理"])
def test_4_business_roles_post_normalize(role_name):
    """4 个改造后的 SE 业务角色 build_system_prompt 走 business_strict 路径不抛错。"""
    from common import build_system_prompt

    static, _ = build_system_prompt(role_name)
    assert len(static) > 0
    # 业务关键内容必入
    if role_name == "后端工程师":
        assert "B2 启动钩子" in static or "lifespan" in static
    if role_name == "前端工程师":
        assert "fetch" in static.lower() or "F1" in static
    if role_name == "技术主管":
        assert "Plan call" in static or "全局约束" in static
    if role_name == "产品经理":
        assert "PRD-输出模板" in static or "需求翻译" in static
    # 版本历史不入
    assert "## 8. 版本历史" not in static


# ── 10. 3 个创意角色 domain=通用 走 business_strict ──────
@pytest.mark.parametrize("role_name", ["创意发散者", "创意质询者", "创意记录员"])
def test_brainstorm_3_roles_business_strict(role_name):
    """T2.7 修正后 3 创意角色 domain=通用，走业务严格路径。"""
    from common import build_system_prompt

    static, _ = build_system_prompt(role_name)
    assert len(static) > 0
    # 验证版本历史段不在
    assert "## 8. 版本历史" not in static
