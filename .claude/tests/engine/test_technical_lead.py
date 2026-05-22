"""
tests/engine/test_technical_lead.py — technical_lead 核心逻辑单元测试

覆盖范围：
  P0 - Plan JSON schema（estimate_hours 字段）
  P0 - _extract_json_block：围栏 / 裸 JSON 两种格式
  P0 - _run_pass_split：后端/前端 Plan 解析路径（mock call_llm）
  P0 - _split_oversized_detail：正常拆分 / 拆分失败回退 / 索引 patch / side 参数
  P0 - Detail 二次拆分触发逻辑（体积阈值 + 工时阈值）
  P0 - 前端侧 Plan+Detail 拆分对称行为
  旧 - _extract_last_round_text：末轮决议提取
  旧 - _read_project_type：frontmatter 解析
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# conftest.py 已经把 .claude/ 和 .claude/skills/ 加入 sys.path
import engine.config as engine_config
from technical_lead import main as tl_mod


# ══════════════════════════════════════════════════════
# 共享 fixture
# ══════════════════════════════════════════════════════

@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 tl_mod.VAULT_ROOT 和 engine.config.VAULT_ROOT 都指向 tmp_path，
    确保 resolve_path 写盘到临时目录而非真实 vault。
    """
    monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    yield tmp_path


# ══════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════

def _write(p: Path, content: str = "placeholder\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ══════════════════════════════════════════════════════
# _extract_json_block
# ══════════════════════════════════════════════════════

class TestExtractJsonBlock:
    def test_fenced_json(self):
        text = '前缀\n```json\n{"a": 1}\n```\n后缀'
        assert tl_mod._extract_json_block(text) == '{"a": 1}'

    def test_fenced_no_lang(self):
        text = '```\n{"x": "y"}\n```'
        assert tl_mod._extract_json_block(text) == '{"x": "y"}'

    def test_bare_json_balanced(self):
        text = '好的，结果如下：{"tasks": [{"id": "T01"}]}'
        result = tl_mod._extract_json_block(text)
        assert json.loads(result) == {"tasks": [{"id": "T01"}]}

    def test_nested_braces(self):
        text = '{"a": {"b": {"c": 1}}}'
        assert tl_mod._extract_json_block(text) == text

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="未找到 JSON 起始"):
            tl_mod._extract_json_block("没有任何 JSON")

    def test_unbalanced_raises(self):
        with pytest.raises(ValueError, match="括号未配平"):
            tl_mod._extract_json_block("{未配平")

    def test_string_with_brace_not_confused(self):
        # 字符串内的 { } 不应影响深度计数
        text = '{"key": "value with { curly }"}'
        result = tl_mod._extract_json_block(text)
        assert json.loads(result)["key"] == "value with { curly }"

    def test_escaped_quote_in_string(self):
        text = r'{"k": "say \"hello\""}'
        result = tl_mod._extract_json_block(text)
        assert json.loads(result)["k"] == 'say "hello"'


# ══════════════════════════════════════════════════════
# _extract_last_round_text
# ══════════════════════════════════════════════════════

class TestExtractLastRoundText:
    def test_single_round(self):
        content = "### 第 1 轮 · 架构师\n决策内容"
        result = tl_mod._extract_last_round_text(content)
        assert result is not None
        assert "决策内容" in result

    def test_picks_highest_round(self):
        content = (
            "### 第 1 轮 · 架构师\n内容1\n\n"
            "### 第 2 轮 · 产品经理\n内容2\n\n"
            "### 第 3 轮 · 架构师\n末轮内容\n"
        )
        result = tl_mod._extract_last_round_text(content)
        assert result is not None
        assert "末轮内容" in result
        assert "内容1" not in result
        assert "内容2" not in result

    def test_returns_none_for_no_heading(self):
        content = "## 普通章节\n内容"
        assert tl_mod._extract_last_round_text(content) is None

    def test_last_round_until_end(self):
        """末轮后无下一轮时应取到文件结尾。"""
        content = "### 第 1 轮 · A\n内容A\n\n### 第 2 轮 · B\n内容B\n末尾行\n"
        result = tl_mod._extract_last_round_text(content)
        assert "末尾行" in result


# ══════════════════════════════════════════════════════
# _read_project_type
# ══════════════════════════════════════════════════════

class TestReadProjectType:
    def _make_file(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "给技术主管.md"
        p.write_text(content, encoding="utf-8")
        return p

    def test_backend_only(self, tmp_path):
        p = self._make_file(tmp_path, "---\nproject_type: backend-only\n---\n内容")
        pt, src = tl_mod._read_project_type(p)
        assert pt == "backend-only"
        assert src == "frontmatter"

    def test_frontend_only(self, tmp_path):
        p = self._make_file(tmp_path, "---\nproject_type: frontend-only\n---\n")
        pt, src = tl_mod._read_project_type(p)
        assert pt == "frontend-only"
        assert src == "frontmatter"

    def test_full_stack(self, tmp_path):
        p = self._make_file(tmp_path, "---\nproject_type: full-stack\n---\n")
        pt, src = tl_mod._read_project_type(p)
        assert pt == "full-stack"
        assert src == "frontmatter"

    def test_missing_field_defaults_to_full_stack(self, tmp_path):
        p = self._make_file(tmp_path, "---\nfrom: 架构师\n---\n内容")
        pt, src = tl_mod._read_project_type(p)
        assert pt == "full-stack"
        assert src == "default_full_stack"

    def test_no_frontmatter_defaults(self, tmp_path):
        p = self._make_file(tmp_path, "# 标题\n内容")
        pt, src = tl_mod._read_project_type(p)
        assert pt == "full-stack"
        assert src == "default_full_stack"

    def test_missing_file_defaults(self, tmp_path):
        pt, src = tl_mod._read_project_type(tmp_path / "不存在.md")
        assert pt == "full-stack"
        assert src == "default_full_stack"

    def test_invalid_type_falls_back_with_warning(self, tmp_path, capsys):
        p = self._make_file(tmp_path, "---\nproject_type: unknown-type\n---\n")
        pt, src = tl_mod._read_project_type(p)
        assert pt == "full-stack"
        assert src == "default_full_stack"
        assert "不在合法集" in capsys.readouterr().err


# ══════════════════════════════════════════════════════
# P0 — Plan JSON schema：estimate_hours 字段
# ══════════════════════════════════════════════════════

class TestPlanPromptEstimateHours:
    """验证 Plan call prompt 中包含 estimate_hours 字段说明。"""

    def test_plan_prompt_contains_estimate_hours(self, tmp_path, monkeypatch):
        """_run_backend_pass_split 构建的 plan_prompt 必须包含 estimate_hours。"""
        # 拦截 call_llm，记录传入的 prompt
        captured_prompts: list[str] = []

        def fake_llm(system_prompt, prompt, model=None, max_tokens=None):
            captured_prompts.append(prompt)
            # 返回有效 Plan JSON
            return json.dumps({
                "tasks": [],
                "index_md_body": "# 无后端任务\n\n无后端业务",
            })

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock-model")
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        proj_dir = tmp_path / "10-项目" / "testproj"
        proj_dir.mkdir(parents=True)

        tl_mod._run_backend_pass_split(
            system_prompt=("sys", "fmt"),
            base_prompt="测试项目\n",
            project="testproj",
            proj_dir=proj_dir,
        )
        plan_prompt = captured_prompts[0]
        assert "estimate_hours" in plan_prompt

    def test_plan_prompt_constraint_text(self, tmp_path, monkeypatch):
        """Plan prompt 应包含 ≤ 4 小时的约束说明。"""
        captured: list[str] = []

        def fake_llm(system_prompt, prompt, model=None, max_tokens=None):
            captured.append(prompt)
            return json.dumps({"tasks": [], "index_md_body": "# 无后端任务"})

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock-model")
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        proj_dir = tmp_path / "10-项目" / "p2"
        proj_dir.mkdir(parents=True)

        tl_mod._run_backend_pass_split(("s", "f"), "base\n", "p2", proj_dir)
        assert "4 小时" in captured[0]


# ══════════════════════════════════════════════════════
# P0 — _run_backend_pass_split：Plan 解析逻辑
# ══════════════════════════════════════════════════════

class TestRunBackendPassSplit:
    def _make_proj(self, tmp_path: Path, project: str = "proj") -> Path:
        proj_dir = tmp_path / "10-项目" / project
        proj_dir.mkdir(parents=True)
        return proj_dir

    def test_empty_tasks_writes_index_returns_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return json.dumps({"tasks": [], "index_md_body": "# 无后端任务\n\n无后端业务"})

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = self._make_proj(tmp_path)
        ok, written = tl_mod._run_backend_pass_split(
            ("sys", "fmt"), "base\n", "proj", proj_dir
        )
        assert ok is True
        assert any("给后端-索引.md" in p for p in written)
        index_path = proj_dir / "指令" / "给后端-索引.md"
        assert index_path.exists()
        assert "无后端任务" in index_path.read_text(encoding="utf-8")

    def test_invalid_json_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")
        monkeypatch.setattr(tl_mod, "call_llm",
                            lambda sp, p, model=None, max_tokens=None: "不是 JSON 内容")
        proj_dir = self._make_proj(tmp_path, "proj2")
        ok, written = tl_mod._run_backend_pass_split(
            ("sys", "fmt"), "base\n", "proj2", proj_dir
        )
        assert ok is False
        assert written == []

    def test_call_llm_exception_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        def raise_llm(sp, p, model=None, max_tokens=None):
            raise RuntimeError("网络超时")

        monkeypatch.setattr(tl_mod, "call_llm", raise_llm)
        proj_dir = self._make_proj(tmp_path, "proj3")
        ok, written = tl_mod._run_backend_pass_split(
            ("sys", "fmt"), "base\n", "proj3", proj_dir
        )
        assert ok is False
        assert written == []

    def test_single_task_calls_detail_and_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        call_count = {"n": 0}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Plan call
                return json.dumps({
                    "tasks": [
                        {"id": "T01", "title": "用户注册", "summary": "注册接口",
                         "estimate_hours": 2}
                    ],
                    "index_md_body": "# 后端索引\n\n| T01 | 用户注册 | - | - |",
                })
            else:
                # Detail call
                return (
                    "<!-- FILE: 10-项目/proj4/指令/给后端-T01.md -->\n"
                    "---\ntask_id: T01\ntitle: 用户注册\nestimate_hours: 2\n---\n"
                    "# T01 用户注册\n\n功能描述...\n"
                    "<!-- /FILE -->"
                )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = self._make_proj(tmp_path, "proj4")
        ok, written = tl_mod._run_backend_pass_split(
            ("sys", "fmt"), "base\n", "proj4", proj_dir
        )
        assert ok is True
        assert call_count["n"] == 2  # Plan + 1 Detail
        assert any("给后端-T01.md" in p for p in written)
        t01 = proj_dir / "指令" / "给后端-T01.md"
        assert t01.exists()
        assert "用户注册" in t01.read_text(encoding="utf-8")

    def test_plan_cache_skips_llm_on_retry(self, tmp_path, monkeypatch):
        """Plan 缓存命中时，LLM 只被调用一次（Detail call）。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        # 预写 plan 缓存（新签名：side 参数）
        cache_path = tl_mod._plan_cache_path("cached_proj", "后端")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cached_plan = {
            "tasks": [
                {"id": "T01", "title": "缓存任务", "summary": "摘要", "estimate_hours": 1}
            ],
            "index_md_body": "# 索引\n\n| T01 | 缓存任务 | - | - |",
        }
        cache_path.write_text(json.dumps(cached_plan, ensure_ascii=False), encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            return (
                "<!-- FILE: 10-项目/cached_proj/指令/给后端-T01.md -->\n"
                "---\ntask_id: T01\n---\n# 缓存任务\n内容\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = self._make_proj(tmp_path, "cached_proj")
        ok, written = tl_mod._run_backend_pass_split(
            ("sys", "fmt"), "base\n", "cached_proj", proj_dir
        )
        assert ok is True
        # 只有 Detail call，Plan call 被跳过
        assert call_count["n"] == 1

    def test_existing_detail_skipped_on_retry(self, tmp_path, monkeypatch):
        """已存在且体积 > 200 的 detail 文件应被跳过，不重调 LLM。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        proj_dir = self._make_proj(tmp_path, "retry_proj")
        # 预写 T01 detail 文件（模拟上次成功的产出）
        existing = proj_dir / "指令" / "给后端-T01.md"
        _write(existing, "---\ntask_id: T01\n---\n# 已存在\n" + "内容" * 100)

        call_count = {"n": 0}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return json.dumps({
                    "tasks": [
                        {"id": "T01", "title": "已存在任务", "summary": "s",
                         "estimate_hours": 2}
                    ],
                    "index_md_body": "# 索引",
                })
            raise AssertionError("Detail call 不应被调用")

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        ok, written = tl_mod._run_pass_split(
            "后端", ("sys", "fmt"), "base\n", "retry_proj", proj_dir
        )
        assert ok is True
        assert any("T01" in p for p in written)
        assert call_count["n"] == 1  # 只有 Plan call


# ══════════════════════════════════════════════════════
# P0 — _split_oversized_detail
# ══════════════════════════════════════════════════════

class TestSplitOversizedDetail:
    def _proj(self, tmp_path: Path, name: str = "sp") -> Path:
        d = tmp_path / "10-项目" / name
        d.mkdir(parents=True)
        return d

    def _index(self, proj_dir: Path, tid: str = "T01") -> Path:
        """写一个含 tid 表格行的伪索引文件。"""
        p = proj_dir / "指令" / "给后端-索引.md"
        _write(p, f"# 后端索引\n\n| {tid} | 任务标题 | 后端 | 3 |\n")
        return p

    def test_successful_split_returns_sub_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        proj_dir = self._proj(tmp_path)
        self._index(proj_dir)

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return (
                "<!-- FILE: 10-项目/sp/指令/给后端-T01a.md -->\n"
                "---\ntask_id: T01a\nestimate_hours: 2\n---\n# 子任务 A\n内容\n<!-- /FILE -->\n"
                "<!-- FILE: 10-项目/sp/指令/给后端-T01b.md -->\n"
                "---\ntask_id: T01b\nestimate_hours: 2\n---\n# 子任务 B\n内容\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)

        result = tl_mod._split_oversized_detail(
            system_prompt=("sys", "fmt"),
            base_prompt="base\n",
            project="sp",
            proj_dir=proj_dir,
            side="后端",
            tid="T01",
            title="大任务",
            original_content="x" * 9000,
            written=[],
        )
        assert any("T01a" in p for p in result)
        assert any("T01b" in p for p in result)
        assert (proj_dir / "指令" / "给后端-T01a.md").exists()
        assert (proj_dir / "指令" / "给后端-T01b.md").exists()

    def test_split_failure_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        proj_dir = self._proj(tmp_path, "sp2")
        self._index(proj_dir)

        def bad_llm(sp, prompt, model=None, max_tokens=None):
            raise RuntimeError("haiku 超时")

        monkeypatch.setattr(tl_mod, "call_llm", bad_llm)

        result = tl_mod._split_oversized_detail(
            ("sys", "fmt"), "base\n", "sp2", proj_dir,
            "后端", "T01", "大任务", "x" * 9000, [],
        )
        assert result == []

    def test_split_no_file_block_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        proj_dir = self._proj(tmp_path, "sp3")
        self._index(proj_dir)

        monkeypatch.setattr(tl_mod, "call_llm",
                            lambda sp, p, model=None, max_tokens=None: "没有 FILE 块的输出")

        result = tl_mod._split_oversized_detail(
            ("sys", "fmt"), "base\n", "sp3", proj_dir,
            "后端", "T01", "大任务", "x" * 9000, [],
        )
        assert result == []

    def test_index_patched_after_split(self, tmp_path, monkeypatch):
        """二次拆分成功后，索引文件中的原 tid 行应被替换为子任务行。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        proj_dir = self._proj(tmp_path, "sp4")
        index_path = self._index(proj_dir, "T02")

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return (
                "<!-- FILE: 10-项目/sp4/指令/给后端-T02a.md -->\n"
                "---\ntask_id: T02a\n---\n# 子A\n内容\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)

        tl_mod._split_oversized_detail(
            ("sys", "fmt"), "base\n", "sp4", proj_dir,
            "后端", "T02", "大任务2", "x" * 9000, [],
        )

        patched = index_path.read_text(encoding="utf-8")
        # 原 T02 行应被替换
        assert "T02a" in patched
        # 不能保留完整原始的 "| T02 | 任务标题" 行
        assert re.search(r"\|\s*T02\s*\|\s*任务标题", patched) is None

    def test_index_unchanged_if_tid_not_in_table(self, tmp_path, monkeypatch):
        """索引中没有对应 tid 行时，索引文件不被修改。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        proj_dir = self._proj(tmp_path, "sp5")
        # 索引里只有 T99，不含 T01
        index_path = proj_dir / "指令" / "给后端-索引.md"
        _write(index_path, "# 索引\n\n| T99 | 其他任务 | 后端 | 1 |\n")
        original = index_path.read_text(encoding="utf-8")

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return (
                "<!-- FILE: 10-项目/sp5/指令/给后端-T01a.md -->\n"
                "---\ntask_id: T01a\n---\n# 子A\n内容\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)

        tl_mod._split_oversized_detail(
            ("sys", "fmt"), "base\n", "sp5", proj_dir,
            "后端", "T01", "大任务", "x" * 9000, [],
        )
        assert index_path.read_text(encoding="utf-8") == original


# ══════════════════════════════════════════════════════
# P0 — 二次拆分触发阈值（集成级，mock call_llm）
# ══════════════════════════════════════════════════════

class TestSplitTrigger:
    """验证在 _run_pass_split 的 Detail 写盘环节正确触发二次拆分（后端/前端对称）。"""

    def _plan_response(self, project: str, estimate_hours: int, side: str = "后端") -> str:
        prefix = f"给{side}"
        return json.dumps({
            "tasks": [
                {"id": "T01", "title": "测试任务", "summary": "摘要",
                 "estimate_hours": estimate_hours}
            ],
            "index_md_body": f"# {side}索引\n\n| T01 | 测试任务 | {side} | {estimate_hours} |\n",
        })

    def _detail_response(self, project: str, side: str = "后端", size: int = 100) -> str:
        prefix = f"给{side}"
        body = "内容" * (size // 2)
        return (
            f"<!-- FILE: 10-项目/{project}/指令/{prefix}-T01.md -->\n"
            f"---\ntask_id: T01\nestimate_hours: 5\n---\n# 测试任务\n{body}\n<!-- /FILE -->"
        )

    def test_hours_over_threshold_triggers_split(self, tmp_path, monkeypatch):
        """estimate_hours > 4 时应触发二次拆分。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        project = "trig1"
        split_called = {"called": False}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            if "estimate_hours" in prompt and "只列" in prompt and "任务大纲" in prompt:
                return self._plan_response(project, estimate_hours=5)
            elif "只产出**一个后端任务**" in prompt:
                return self._detail_response(project, size=100)  # 体积小，但工时超
            else:
                split_called["called"] = True
                return (
                    f"<!-- FILE: 10-项目/{project}/指令/给后端-T01a.md -->\n"
                    "---\ntask_id: T01a\nestimate_hours: 2\ntitle: 子A\n---\n# 子A\n内容\n<!-- /FILE -->\n"
                    f"<!-- FILE: 10-项目/{project}/指令/给后端-T01b.md -->\n"
                    "---\ntask_id: T01b\nestimate_hours: 3\ntitle: 子B\n---\n# 子B\n内容\n<!-- /FILE -->"
                )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = tmp_path / "10-项目" / project
        proj_dir.mkdir(parents=True)

        ok, written = tl_mod._run_pass_split(
            "后端", ("sys", "fmt"), "base\n", project, proj_dir
        )
        assert ok is True
        assert split_called["called"], "estimate_hours=5 应触发二次拆分"
        assert not (proj_dir / "指令" / "给后端-T01.md").exists()
        assert (proj_dir / "指令" / "给后端-T01a.md").exists()

    def test_size_over_threshold_triggers_split(self, tmp_path, monkeypatch):
        """文件体积 > 8000 chars 时应触发二次拆分（即便 estimate_hours ≤ 4）。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        project = "trig2"
        split_called = {"called": False}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            if "只列" in prompt and "任务大纲" in prompt:
                return self._plan_response(project, estimate_hours=3)
            elif "只产出**一个后端任务**" in prompt:
                big_body = "内容描述" * 3000  # ~12000 chars
                return (
                    f"<!-- FILE: 10-项目/{project}/指令/给后端-T01.md -->\n"
                    f"---\ntask_id: T01\nestimate_hours: 3\n---\n# 大任务\n{big_body}\n<!-- /FILE -->"
                )
            else:
                split_called["called"] = True
                return (
                    f"<!-- FILE: 10-项目/{project}/指令/给后端-T01a.md -->\n"
                    "---\ntask_id: T01a\nestimate_hours: 2\ntitle: 子A\n---\n# 子A\n内容\n<!-- /FILE -->"
                )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = tmp_path / "10-项目" / project
        proj_dir.mkdir(parents=True)

        ok, written = tl_mod._run_pass_split(
            "后端", ("sys", "fmt"), "base\n", project, proj_dir
        )
        assert ok is True
        assert split_called["called"], "体积 > 8000 chars 应触发二次拆分"

    def test_within_thresholds_no_split(self, tmp_path, monkeypatch):
        """estimate_hours ≤ 4 且体积 ≤ 8000 时不触发二次拆分。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        project = "trig3"
        split_called = {"called": False}
        call_count = {"n": 0}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return self._plan_response(project, estimate_hours=2)
            elif call_count["n"] == 2:
                return (
                    f"<!-- FILE: 10-项目/{project}/指令/给后端-T01.md -->\n"
                    "---\ntask_id: T01\nestimate_hours: 2\n---\n# 普通任务\n"
                    "内容不超阈值\n<!-- /FILE -->"
                )
            else:
                split_called["called"] = True
                return ""

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = tmp_path / "10-项目" / project
        proj_dir.mkdir(parents=True)

        ok, written = tl_mod._run_pass_split(
            "后端", ("sys", "fmt"), "base\n", project, proj_dir
        )
        assert ok is True
        assert not split_called["called"], "不应触发二次拆分"
        assert (proj_dir / "指令" / "给后端-T01.md").exists()


# ══════════════════════════════════════════════════════
# P0 — 前端侧 Plan+Detail 对称性测试
# ══════════════════════════════════════════════════════

class TestFrontendPassSplit:
    """验证前端侧 _run_pass_split 行为与后端对称。"""

    def test_frontend_empty_tasks_writes_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return json.dumps({"tasks": [], "index_md_body": "# 无前端任务\n\n纯后端项目"})

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = tmp_path / "10-项目" / "fproj"
        proj_dir.mkdir(parents=True)

        ok, written = tl_mod._run_pass_split(
            "前端", ("sys", "fmt"), "base\n", "fproj", proj_dir
        )
        assert ok is True
        index = proj_dir / "指令" / "给前端-索引.md"
        assert index.exists()
        assert "无前端任务" in index.read_text(encoding="utf-8")
        assert any("给前端-索引.md" in p for p in written)

    def test_frontend_single_task_writes_detail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")

        project = "fproj2"
        call_count = {"n": 0}

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return json.dumps({
                    "tasks": [{"id": "T01", "title": "登录页", "summary": "实现登录 UI",
                               "estimate_hours": 3}],
                    "index_md_body": "# 前端索引\n\n| T01 | 登录页 | 3h | [] |",
                })
            else:
                return (
                    f"<!-- FILE: 10-项目/{project}/指令/给前端-T01.md -->\n"
                    "---\ntask_id: T01\ntitle: 登录页\nrole: 前端工程师\n"
                    "estimate_hours: 3\n---\n# 登录页\n实现内容\n<!-- /FILE -->"
                )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        proj_dir = tmp_path / "10-项目" / project
        proj_dir.mkdir(parents=True)

        ok, written = tl_mod._run_pass_split(
            "前端", ("sys", "fmt"), "base\n", project, proj_dir
        )
        assert ok is True
        assert call_count["n"] == 2  # Plan + 1 Detail
        t01 = proj_dir / "指令" / "给前端-T01.md"
        assert t01.exists()
        assert "登录页" in t01.read_text(encoding="utf-8")

    def test_frontend_plan_cache_independent_from_backend(self, tmp_path, monkeypatch):
        """前后端 Plan 缓存 key 互不干扰。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        backend_cache = tl_mod._plan_cache_path("myproj", "后端")
        frontend_cache = tl_mod._plan_cache_path("myproj", "前端")
        assert backend_cache != frontend_cache
        assert "后端" in backend_cache.name
        assert "前端" in frontend_cache.name

    def test_frontend_split_patches_frontend_index(self, tmp_path, monkeypatch):
        """前端侧二次拆分应 patch 「给前端-索引.md」而非「给后端-索引.md」。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        proj_dir = tmp_path / "10-项目" / "fsplit"
        # 写前端索引（含 T01 行）
        fe_index = proj_dir / "指令" / "给前端-索引.md"
        _write(fe_index, "# 前端索引\n\n| T01 | 大前端任务 | 6h | [] |\n")
        # 写后端索引（不应被修改）
        be_index = proj_dir / "指令" / "给后端-索引.md"
        _write(be_index, "# 后端索引\n\n| T01 | 后端任务 | 2h | [] |\n")
        be_original = be_index.read_text(encoding="utf-8")

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return (
                "<!-- FILE: 10-项目/fsplit/指令/给前端-T01a.md -->\n"
                "---\ntask_id: T01a\ntitle: 前端子A\nestimate_hours: 2\n---\n# 前端子A\n内容\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)

        tl_mod._split_oversized_detail(
            ("sys", "fmt"), "base\n", "fsplit", proj_dir,
            "前端", "T01", "大前端任务", "x" * 9000, [],
        )

        fe_patched = fe_index.read_text(encoding="utf-8")
        assert "T01a" in fe_patched
        assert re.search(r"\|\s*T01\s*\|\s*大前端任务", fe_patched) is None
        # 后端索引不变
        assert be_index.read_text(encoding="utf-8") == be_original


# ══════════════════════════════════════════════════════
# Dynamic skill_refs injection in Detail call loop
# ══════════════════════════════════════════════════════

class TestDynamicSkillInjection:
    """验证 _run_pass_split Detail call 中 skill_refs 动态裁剪逻辑。"""

    def _make_plan(self, summary: str, estimate_hours: int = 2) -> dict:
        return {
            "tasks": [{"id": "T01", "title": "任务A", "summary": summary, "estimate_hours": estimate_hours}],
            "index_md_body": "---\ntype: task-index\n---\n# 索引\n\n| T01 | 任务A | 2h | [] |\n",
        }

    def test_skill_block_injected_when_wikilinks_present(self, tmp_path, monkeypatch):
        """summary 含有 wikilink 时，detail_system_prompt 应含 skill_block。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        captured_prompts: list[tuple] = []

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            captured_prompts.append(sp)
            return (
                "<!-- FILE: 10-项目/dyntest/指令/给后端-T01.md -->\n"
                "---\ntask_id: T01\ntitle: 任务A\nrole: 后端工程师\n"
                "from: 技术主管\nto: 后端工程师\nestimate_hours: 2\n"
                "depends_on: []\nunblocks: []\ncreated: 2026-01-01\n---\n# 任务A\n内容\n<!-- /FILE -->"
            )

        def fake_build_task_skill_block(stems, side, **kwargs):
            return "\n\n## 本任务相关技能（按 summary wikilink 动态加载）\n\n=== Skill: [[B5-空集守卫]] ===\n技能内容\n"

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        monkeypatch.setattr(tl_mod, "build_task_skill_block", fake_build_task_skill_block)

        proj_dir = tmp_path / "10-项目" / "dyntest"
        plan = self._make_plan("实现接口 [[B5-空集守卫]]，处理空集场景")
        plan_json = json.dumps(plan, ensure_ascii=False)
        plan_cache = tmp_path / "00-系统" / ".runtime-state" / "技术主管.plan-后端-dyntest.json"
        plan_cache.parent.mkdir(parents=True, exist_ok=True)
        plan_cache.write_text(plan_json, encoding="utf-8")

        ok, written = tl_mod._run_pass_split(
            "后端",
            ("static_sys", "dynamic_sys"),
            "base\n",
            "dyntest",
            proj_dir,
        )

        assert ok
        # Detail call 使用了含 skill block 的 system prompt
        assert len(captured_prompts) == 1
        detail_sp = captured_prompts[0]
        assert "本任务相关技能" in detail_sp[1] or "本任务相关技能" in detail_sp[0]

    def test_skill_block_skipped_when_no_wikilinks(self, tmp_path, monkeypatch):
        """summary 不含 wikilink 时，system_prompt 应原样传递给 detail call。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        captured_prompts: list[tuple] = []

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            captured_prompts.append(sp)
            return (
                "<!-- FILE: 10-项目/dyntest2/指令/给后端-T01.md -->\n"
                "---\ntask_id: T01\ntitle: 任务B\nrole: 后端工程师\n"
                "from: 技术主管\nto: 后端工程师\nestimate_hours: 2\n"
                "depends_on: []\nunblocks: []\ncreated: 2026-01-01\n---\n# 任务B\n内容\n<!-- /FILE -->"
            )

        skill_block_called = []

        def fake_build_task_skill_block(stems, side, **kwargs):
            skill_block_called.append(stems)
            return ""

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        monkeypatch.setattr(tl_mod, "build_task_skill_block", fake_build_task_skill_block)

        proj_dir = tmp_path / "10-项目" / "dyntest2"
        plan = self._make_plan("普通任务描述，没有 wikilink")
        plan_json = json.dumps(plan, ensure_ascii=False)
        plan_cache = tmp_path / "00-系统" / ".runtime-state" / "技术主管.plan-后端-dyntest2.json"
        plan_cache.parent.mkdir(parents=True, exist_ok=True)
        plan_cache.write_text(plan_json, encoding="utf-8")

        ok, written = tl_mod._run_pass_split(
            "后端",
            ("static_sys", "dynamic_sys"),
            "base\n",
            "dyntest2",
            proj_dir,
        )

        assert ok
        # build_task_skill_block 不应被调用（summary 无 wikilink）
        assert skill_block_called == []
        # system_prompt 原样传递
        assert len(captured_prompts) == 1
        assert captured_prompts[0] == ("static_sys", "dynamic_sys")

    def test_skill_block_uses_side_filter(self, tmp_path, monkeypatch):
        """只有与 side 匹配的 skill 前缀才应传入 build_task_skill_block。"""
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        received_args: list[tuple] = []

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return (
                "<!-- FILE: 10-项目/sidetest/指令/给前端-T01.md -->\n"
                "---\ntask_id: T01\ntitle: 前端任务\nrole: 前端工程师\n"
                "from: 技术主管\nto: 前端工程师\nestimate_hours: 2\n"
                "depends_on: []\nunblocks: []\ncreated: 2026-01-01\n---\n# 前端任务\n内容\n<!-- /FILE -->"
            )

        def fake_build_task_skill_block(stems, side, **kwargs):
            received_args.append((stems, side))
            return ""

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        monkeypatch.setattr(tl_mod, "build_task_skill_block", fake_build_task_skill_block)

        proj_dir = tmp_path / "10-项目" / "sidetest"
        plan = {
            "tasks": [{"id": "T01", "title": "前端任务", "summary": "实现 [[F3-状态管理]] 模式", "estimate_hours": 2}],
            "index_md_body": "---\ntype: task-index\n---\n# 前端索引\n\n| T01 | 前端任务 | 2h | [] |\n",
        }
        plan_cache = tmp_path / "00-系统" / ".runtime-state" / "技术主管.plan-前端-sidetest.json"
        plan_cache.parent.mkdir(parents=True, exist_ok=True)
        plan_cache.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

        ok, _ = tl_mod._run_pass_split(
            "前端",
            ("static_sys", "dynamic_sys"),
            "base\n",
            "sidetest",
            proj_dir,
        )

        assert ok
        assert len(received_args) == 1
        stems, side = received_args[0]
        assert side == "前端"
        assert "F3-状态管理" in stems


# ══════════════════════════════════════════════════════
# 方案 A（2026-05-21）— Plan 协议去 index_md_body + 模板渲染
# ══════════════════════════════════════════════════════

class TestNormalizeTask:
    def test_minimal_valid(self):
        assert tl_mod._normalize_task({"id": "T01", "title": "x"}) == {
            "id": "T01", "title": "x", "summary": "", "estimate_hours": 0, "depends_on": [],
        }

    def test_depends_on_list(self):
        out = tl_mod._normalize_task({"id": "T01", "title": "x", "depends_on": ["T01", "T02"]})
        assert out["depends_on"] == ["T01", "T02"]

    def test_depends_on_string_split(self):
        out = tl_mod._normalize_task({"id": "T01", "title": "x", "depends_on": "T01, T02"})
        assert out["depends_on"] == ["T01", "T02"]

    def test_depends_on_unexpected_type_defaults_empty(self):
        out = tl_mod._normalize_task({"id": "T01", "title": "x", "depends_on": 42})
        assert out["depends_on"] == []

    def test_missing_id_returns_none(self):
        assert tl_mod._normalize_task({"title": "x"}) is None

    def test_missing_title_returns_none(self):
        assert tl_mod._normalize_task({"id": "T01"}) is None

    def test_non_dict_returns_none(self):
        assert tl_mod._normalize_task("not a dict") is None
        assert tl_mod._normalize_task(None) is None


class TestRenderIndexMd:
    def test_empty_tasks_with_reason(self):
        md = tl_mod._render_index_md("后端", "demo", [], skip_reason="纯前端项目")
        assert "# 无后端任务" in md
        assert "纯前端项目" in md
        assert "status: skipped" in md

    def test_empty_tasks_default_reason(self):
        md = tl_mod._render_index_md("前端", "demo", [], skip_reason="")
        assert "# 无前端任务" in md

    def test_single_task_renders_table(self):
        tasks = [
            {"id": "T01", "title": "登录页", "summary": "", "estimate_hours": 3, "depends_on": []},
        ]
        md = tl_mod._render_index_md("前端", "demo", tasks)
        assert "| T01 | 登录页 | 3 | — |" in md
        assert "总工时：3 h" in md
        assert "[[给前端-T01]]" in md
        assert "status: ready" in md

    def test_multiple_tasks_with_deps(self):
        tasks = [
            {"id": "T01", "title": "A", "summary": "", "estimate_hours": 2, "depends_on": []},
            {"id": "T02", "title": "B", "summary": "", "estimate_hours": 3, "depends_on": ["T01"]},
            {"id": "T03", "title": "C", "summary": "", "estimate_hours": 4, "depends_on": ["T01", "T02"]},
        ]
        md = tl_mod._render_index_md("后端", "demo", tasks)
        assert "| T01 | A | 2 | — |" in md
        assert "| T02 | B | 3 | T01 |" in md
        assert "| T03 | C | 4 | T01, T02 |" in md
        assert "总工时：9 h" in md

    def test_more_than_5_tasks_truncates_wikilinks(self):
        tasks = [
            {"id": f"T0{i}", "title": f"任务{i}", "summary": "", "estimate_hours": 1, "depends_on": []}
            for i in range(1, 8)
        ]
        md = tl_mod._render_index_md("后端", "demo", tasks)
        # 表格全列，wikilink 列表截断到 5 + 省略号
        assert "[[给后端-T01]]" in md
        assert "[[给后端-T05]]" in md
        assert "…" in md  # 省略号
        # 但表格行全在
        for i in range(1, 8):
            assert f"| T0{i} |" in md


class TestPlanPromptNoIndexMdBody:
    """方案 A：Plan prompt 不再要求 LLM 输出 index_md_body 字段。"""

    def test_plan_prompt_omits_index_md_body(self, tmp_path, monkeypatch):
        captured: list[str] = []

        def fake_llm(sp, prompt, model=None, max_tokens=None):
            captured.append(prompt)
            return json.dumps({"tasks": []})

        monkeypatch.setattr(tl_mod, "call_llm", fake_llm)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock-model")
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        proj_dir = tmp_path / "10-项目" / "p"
        proj_dir.mkdir(parents=True)
        tl_mod._run_backend_pass_split(("s", "f"), "base\n", "p", proj_dir)

        assert captured, "Plan call 未触发"
        prompt = captured[0]
        assert "index_md_body" not in prompt
        # 但仍要求 depends_on 字段（新增）
        assert "depends_on" in prompt

    def test_index_rendered_from_template_when_llm_omits(self, tmp_path, monkeypatch):
        """LLM 完全不返回 index 信息也能生成索引（模板兜底）。"""
        def fake_llm(sp, prompt, model=None, max_tokens=None):
            return json.dumps({
                "tasks": [
                    {"id": "T01", "title": "建表", "summary": "s", "estimate_hours": 2,
                     "depends_on": []},
                ],
            })

        # Detail call 也走同一 fake_llm，需要返回 FILE 块
        call_count = {"n": 0}

        def routed_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return fake_llm(sp, prompt)
            return (
                "<!-- FILE: 10-项目/tmpl/指令/给后端-T01.md -->\n"
                "---\ntask_id: T01\nestimate_hours: 2\n---\n# T01 建表\n详情\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", routed_llm)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        proj_dir = tmp_path / "10-项目" / "tmpl"
        proj_dir.mkdir(parents=True)

        ok, written = tl_mod._run_pass_split(
            "后端", ("s", "f"), "base\n", "tmpl", proj_dir,
        )
        assert ok
        idx = proj_dir / "指令" / "给后端-索引.md"
        assert idx.exists()
        body = idx.read_text(encoding="utf-8")
        assert "| T01 | 建表 | 2 | — |" in body
        assert "status: ready" in body

    def test_unparseable_index_in_llm_response_does_not_break_flow(self, tmp_path, monkeypatch):
        """方案 A 的核心保护：LLM 即便给 markdown 字符串里夹 ASCII 双引号也不会崩。

        旧实现：LLM 输出 index_md_body 含未转义 `"` → JSON.loads 失败 → fallback。
        新实现：根本不要求 LLM 输出 markdown → 不存在转义风险。
        """
        call_count = {"n": 0}

        def routed_llm(sp, prompt, model=None, max_tokens=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # LLM 多输出一个无关字段，且字段值含 ASCII 引号，但因为我们不再
                # 把 markdown 塞进 JSON 字符串，引号在 JSON 字符串里只占两个字符。
                return json.dumps({
                    "tasks": [
                        {"id": "T01", "title": '处理"边界"场景', "summary": "",
                         "estimate_hours": 1, "depends_on": []},
                    ],
                    "noise_field": '可以含 "ASCII 引号" 也没事',
                })
            return (
                "<!-- FILE: 10-项目/q/指令/给后端-T01.md -->\n"
                "---\ntask_id: T01\nestimate_hours: 1\n---\n# x\n内容\n<!-- /FILE -->"
            )

        monkeypatch.setattr(tl_mod, "call_llm", routed_llm)
        monkeypatch.setattr(tl_mod, "_resolve_role_model", lambda: "mock")
        monkeypatch.setattr(tl_mod, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)

        proj_dir = tmp_path / "10-项目" / "q"
        proj_dir.mkdir(parents=True)
        ok, written = tl_mod._run_pass_split(
            "后端", ("s", "f"), "base\n", "q", proj_dir,
        )
        assert ok
        idx = proj_dir / "指令" / "给后端-索引.md"
        assert idx.exists()
        # 标题被原样保留，但因为是模板渲染，markdown 表格里 ASCII 引号也无害
        assert '处理"边界"场景' in idx.read_text(encoding="utf-8")
