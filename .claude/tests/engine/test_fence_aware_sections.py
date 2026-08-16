"""
tests/engine/test_fence_aware_sections.py
    —— 章节抽取的代码围栏感知（2026-08-16 修）

## 背景：本项目最大的一次静默失效

规则文档普遍用 ```markdown 块展示**产出模板**，模板自带 `## 1. xxx` 标题。
`wikilink.extract_section` 与 `input_reader._extract_sections` 都按行找 `#`
前缀且**不识别围栏**，于是模板标题被当成文档同级 H2 —— 抽取当场中止。

实测后果（修复前，`00-系统/规则/music/产物schema.md`）：
- 36 条章节级 `rule_refs` 里 **14 条丢失 > 30%**
- 合计应注入 21531 chars，实际 4525 —— **丢 79%**
- 最严重「4. 曲作.md」只注入 79/1088 chars（**93%**），LLM 拿到的"产物契约"
  实为「标题 + 一行位置 + 一个空的 ```markdown 开头」
- 且 `hit=True` → **不报错、不回退全文、零告警**

这直接解释了两个长期现象：音乐域产物反复偏离 schema；A1（章节注入）只到
69% 章节命中而 A2（改 user_prompt）能到 100% —— A2 绕开了这条坏路径。

## 同一 bug 修过一次却没传播

`role_auditor._split_sections` 早在 2026-08-13 就修了围栏跳过，注释明写
「不跳围栏的话模板里的 `## 1.` 会把真正的 §1 内容整个覆盖掉」。但修法没传到
另外两处。本次收口为**单一实现** `wikilink.iter_lines_with_fence_state`，
三处共用（参 [[feedback_contract_three_layers]]）。

## 数据侧也坏了

源文档用 3 反引号包 3 反引号（CommonMark 无法嵌套），导致 §9/§11/§13 的
**章节标题自己落在围栏里** —— 在 Obsidian 里本来就渲染成一整个代码块。
故同批修数据：`产物schema.md` 4 对外层围栏加宽到 4 反引号、
`流派primitive-schema.md` 补 1 个缺失的闭合。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from engine.wikilink import extract_section, iter_lines_with_fence_state

VAULT = Path(os.environ.get("VAULT_ROOT", r"D:\MarkDown\memory\adam"))


def _states(text: str) -> list[bool]:
    return [f for _, f in iter_lines_with_fence_state(text.splitlines(keepends=True))]


# ══════════════════════════════════════════════════════════════
#  iter_lines_with_fence_state —— 围栏状态机
# ══════════════════════════════════════════════════════════════

class TestFenceState:
    def test_基本围栏(self):
        assert _states("a\n```\nb\n```\nc\n") == [False, True, True, True, False]

    def test_带info的开启与裸闭合(self):
        assert _states("```python\nx\n```\ny\n") == [True, True, True, False]

    def test_带info的行不能闭合围栏(self):
        """CommonMark：闭合行不得带 info string。这正是同宽嵌套错位的根源。"""
        assert _states("```markdown\n```text\nx\n") == [True, True, True]

    def test_四反引号可包三反引号(self):
        """本次数据修复采用的写法：外层 4 反引号，内层 3 反引号正常嵌套。"""
        src = "````markdown\n## 模板标题\n```text\nx\n```\n````\n## 真章节\n"
        assert _states(src) == [True, True, True, True, True, True, False]

    def test_短闭合不能关长围栏(self):
        assert _states("````\n```\nx\n") == [True, True, True]

    def test_波浪号围栏(self):
        assert _states("~~~\nx\n~~~\ny\n") == [True, True, True, False]

    def test_不同字符不互相闭合(self):
        assert _states("```\n~~~\nx\n") == [True, True, True]

    def test_未闭合围栏延续到文末(self):
        assert _states("```\na\nb\n") == [True, True, True]

    def test_缩进最多三格仍算围栏(self):
        assert _states("   ```\nx\n") == [True, True]


# ══════════════════════════════════════════════════════════════
#  extract_section —— 回归：模板标题不得截断抽取
# ══════════════════════════════════════════════════════════════

_DOC = """## 1. 甲产物

**位置**：`x/甲.md`

### 必填章节

````markdown
# 甲：{标题}

## 1. 概述
{内容}

## 2. 细节
```text
示例
```

## 3. 自检
{内容}
````

### 字段校验

- 必填三节

## 2. 乙产物

无关内容
"""


class TestExtractSectionRegression:
    def test_模板标题不再截断抽取(self):
        """修复前只能拿到「标题 + 位置 + ```markdown」三行。"""
        got, hit = extract_section(_DOC, "1. 甲产物")
        assert hit is True
        for must in ("## 1. 概述", "## 2. 细节", "## 3. 自检", "### 字段校验", "必填三节"):
            assert must in got, f"丢失 {must}"

    def test_不越界到下一个文档章节(self):
        got, _ = extract_section(_DOC, "1. 甲产物")
        assert "## 2. 乙产物" not in got and "无关内容" not in got

    def test_内层示例块原样保留(self):
        got, _ = extract_section(_DOC, "1. 甲产物")
        assert "```text" in got and "示例" in got

    def test_未命中仍回退全文(self):
        got, hit = extract_section(_DOC, "不存在的章节")
        assert hit is False and got == _DOC


class TestInputReaderSharesImplementation:
    """input_reader 必须复用同一实现，不得再自带一份（修一处漏一处的老病）。"""

    def test_input_reader_同样围栏感知(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills"))
        from input_reader import _extract_sections
        got = _extract_sections(_DOC, ["1. 甲产物"])
        assert "## 3. 自检" in got and "必填三节" in got
        assert "无关内容" not in got


# ══════════════════════════════════════════════════════════════
#  真实数据守卫：vault 里所有章节级 rule_refs 必须完整可抽
# ══════════════════════════════════════════════════════════════

def _iter_section_refs():
    gene = VAULT / "00-系统" / "角色基因"
    if not gene.is_dir():
        return
    for p in gene.rglob("角色-*.md"):
        m = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", p.read_text(encoding="utf-8"), re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        for r in (fm.get("rule_refs") or []):
            stem, _, sec = str(r).strip().strip("[]").partition("#")
            if sec:
                yield p.stem[3:], stem.strip(), sec.strip()


@pytest.mark.skipif(not VAULT.is_dir(), reason="vault 不可达")
class TestVaultSectionRefsIntact:
    """既守代码回归，也守数据回归 —— 有人再往规则文档里塞同宽嵌套围栏就会红。"""

    def test_不是真空(self):
        assert len(list(_iter_section_refs())) >= 30, "扫不到章节级 rule_refs，守卫失效"

    def test_全部章节可解析且不被截断(self):
        bad = []
        for role, stem, sec in _iter_section_refs():
            hits = list(VAULT.glob(f"00-系统/规则/**/{stem}.md")) or list(VAULT.glob(f"**/{stem}.md"))
            if not hits:
                continue
            body = hits[0].read_text(encoding="utf-8")
            got, _ = extract_section(body, sec)
            # 真实全节：同样用围栏感知切，作为期望值
            start, ref = False, []
            for ln, f in iter_lines_with_fence_state(body.splitlines(keepends=True)):
                if not f and ln.startswith("## "):
                    if start:
                        break
                    if sec.lower() in ln[3:].strip().lower():
                        start = True
                if start:
                    ref.append(ln)
            ref_text = "".join(ref)
            if not ref_text:
                bad.append(f"{role} :: {stem}#{sec} —— 章节不存在")
            elif len(got) < len(ref_text) * 0.9:
                bad.append(
                    f"{role} :: {stem}#{sec} —— 丢 {100 * (1 - len(got) / len(ref_text)):.0f}%"
                )
        assert not bad, "章节抽取异常：\n" + "\n".join(bad)

    def test_规则文档不得含同宽嵌套围栏(self):
        """数据侧守卫：3 反引号里再开 3 反引号 → CommonMark 会错位配对。"""
        fence = re.compile(r"^ {0,3}(`{3,})[ \t]*(.*)$")
        bad = []
        for stem in {s for _, s, _ in _iter_section_refs()}:
            hits = list(VAULT.glob(f"00-系统/规则/**/{stem}.md")) or list(VAULT.glob(f"**/{stem}.md"))
            if not hits:
                continue
            stack: list[int] = []
            for i, ln in enumerate(hits[0].read_text(encoding="utf-8").split("\n"), 1):
                m = fence.match(ln)
                if not m:
                    continue
                if m.group(2).strip():
                    # 安全嵌套要求内层**更短**：内层的闭合行长度不足以关掉外层。
                    # 内层 ≥ 外层时，内层那个裸 ``` 会把外层一并关掉 → 后续标题
                    # 全部错位（2026-08-16 §9/§11/§13 标题被吞的成因）。
                    if stack and len(m.group(1)) >= stack[-1]:
                        bad.append(
                            f"{stem}:L{i} 内层围栏({len(m.group(1))}) ≥ 外层({stack[-1]})"
                            f" → 外层须加宽到更多反引号"
                        )
                    stack.append(len(m.group(1)))
                elif stack:
                    stack.pop()
        assert not bad, "\n".join(bad)
