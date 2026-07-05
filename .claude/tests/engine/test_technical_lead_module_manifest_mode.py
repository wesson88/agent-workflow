"""
test_technical_lead_module_manifest_mode.py — P8.7 补测：technical_lead skill
`_run_module_manifest_mode` 单 call 分支覆盖。

依赖 mock 策略：
- call_claude → monkeypatch 返回不同 raw_output 场景
- write_output_atomic → 拦截写入，不真落盘
- resolve_path → 直接把 rel_path 拼到 tmp 下（避开 VAULT_ROOT / PROJECT_CODE_ROOT）
- set_role_status / append_audit → no-op（no vault side effect）

覆盖分支：
- return 0：raw_output 含 模块清单.md + 模块详情 → 成功
- return 1：call_claude 抛异常
- return 3：raw_output 无 FILE 块 → 落 raw-dump + failed
- return 4：raw_output 有 FILE 块但缺 `模块清单.md` → manifest_missing failed
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))
sys.path.insert(0, str(_SKILLS_DIR / "technical_lead"))


def _import_tl_main():
    """加载 technical_lead/main.py 为独立 module，避免 pytest collect 干扰。"""
    path = _SKILLS_DIR / "technical_lead" / "main.py"
    spec = importlib.util.spec_from_file_location("tl_main_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tl_main():
    return _import_tl_main()


@pytest.fixture
def sample_raw_output_ok():
    """含 `模块清单.md` + 1 个模块详情的合法 raw_output。"""
    return (
        "<!-- FILE: 10-项目/demo/模块清单.md -->\n"
        "# 模块清单\n\n"
        "## 结构化（DAG 原始数据，引擎消费）\n\n"
        "```yaml\n"
        "nodes:\n"
        "  - id: T01\n"
        "    title: 骨架\n"
        "    role: backend\n"
        "    depends_on: []\n"
        "    status: pending\n"
        "```\n"
        "<!-- /FILE -->\n\n"
        "<!-- FILE: 10-项目/demo/模块/T01-骨架.md -->\n"
        "# 模块 T01\n"
        "<!-- /FILE -->\n"
    )


@pytest.fixture
def sample_raw_output_missing_manifest():
    """有 FILE 块但没有 `模块清单.md` —— 缺关键产物。"""
    return (
        "<!-- FILE: 10-项目/demo/模块/T01-骨架.md -->\n"
        "# 模块 T01\n"
        "<!-- /FILE -->\n"
    )


class _StubPatch:
    """给 _run_module_manifest_mode 做外部依赖 mock 的上下文集合。"""

    def __init__(self, tl_main, monkeypatch, tmp_path: Path):
        self.tl_main = tl_main
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.writes: list[tuple[Path, str]] = []
        self.audits: list[dict] = []
        self.status_calls: list[dict] = []

    def apply(self, call_claude_result=None, call_claude_exc=None) -> None:
        m = self.tl_main

        # call_claude：raise 或 return 指定 raw_output
        def _fake_call_claude(system, user, role):
            if call_claude_exc is not None:
                raise call_claude_exc
            return call_claude_result

        self.monkeypatch.setattr(m, "call_claude", _fake_call_claude)

        # write_output_atomic：只记录，不落盘
        def _fake_write(dest, content):
            self.writes.append((Path(dest), content))
        self.monkeypatch.setattr(m, "write_output_atomic", _fake_write)

        # resolve_path：把 rel 拼到 tmp（避 VAULT_ROOT / PROJECT_CODE_ROOT 依赖）
        def _fake_resolve(rel, project):
            return self.tmp_path / Path(rel)
        self.monkeypatch.setattr(m, "resolve_path", _fake_resolve)

        # set_role_status：记录调用不真改 vault runtime state
        def _fake_set_status(role, **kwargs):
            self.status_calls.append({"role": role, **kwargs})
        self.monkeypatch.setattr(m, "set_role_status", _fake_set_status)

        def _fake_audit(entry):
            self.audits.append(entry)
        self.monkeypatch.setattr(m, "append_audit", _fake_audit)

        # enforce_output_limits：no-op（避免调 haiku 压缩链路）
        def _fake_enforce(content, role, filename, limit, **kw):
            return content
        self.monkeypatch.setattr(m, "enforce_output_limits", _fake_enforce)


class TestRunModuleManifestMode:
    def test_success_return_0(
        self, tl_main, monkeypatch, tmp_path, sample_raw_output_ok
    ):
        stub = _StubPatch(tl_main, monkeypatch, tmp_path)
        stub.apply(call_claude_result=sample_raw_output_ok)

        proj_dir = tmp_path / "vault" / "10-项目" / "demo"
        proj_dir.mkdir(parents=True)

        rc = tl_main._run_module_manifest_mode(
            project="demo",
            task="build todo api",
            proj_dir=proj_dir,
            system_prompt=("STATIC", "DYNAMIC"),
            base_prompt="BASE_PROMPT\n",
        )
        assert rc == 0
        # 模块清单.md + 1 个模块详情 → 2 次写
        assert len(stub.writes) == 2
        names = sorted(p.name for p, _ in stub.writes)
        assert "模块清单.md" in names
        assert "T01-骨架.md" in names
        # audit success
        assert any(a["result"] == "success" for a in stub.audits)
        assert any(a["mode"] == "module_manifest" for a in stub.audits)

    def test_call_claude_exception_return_1(
        self, tl_main, monkeypatch, tmp_path
    ):
        stub = _StubPatch(tl_main, monkeypatch, tmp_path)
        stub.apply(call_claude_exc=RuntimeError("API down"))

        proj_dir = tmp_path / "vault" / "10-项目" / "demo"
        proj_dir.mkdir(parents=True)

        rc = tl_main._run_module_manifest_mode(
            project="demo",
            task="build todo api",
            proj_dir=proj_dir,
            system_prompt=("STATIC", "DYNAMIC"),
            base_prompt="BASE\n",
        )
        assert rc == 1
        assert stub.writes == []
        assert any(a["result"] == "failed" for a in stub.audits)
        assert any(a.get("error", "").startswith("API down") for a in stub.audits)

    def test_no_file_blocks_return_3(
        self, tl_main, monkeypatch, tmp_path
    ):
        stub = _StubPatch(tl_main, monkeypatch, tmp_path)
        # raw_output 只是对话文本，没有 <!-- FILE: --> marker
        stub.apply(call_claude_result="Sure, here's the manifest ...（无 marker）")

        proj_dir = tmp_path / "vault" / "10-项目" / "demo"
        (proj_dir / "指令").mkdir(parents=True)

        rc = tl_main._run_module_manifest_mode(
            project="demo",
            task="build",
            proj_dir=proj_dir,
            system_prompt=("STATIC", "DYNAMIC"),
            base_prompt="BASE\n",
        )
        assert rc == 3
        # raw-dump 落盘（用于诊断）
        assert any("技术主管-raw-dump.md" in p.name for p, _ in stub.writes)
        assert any(a["error"] == "no_file_blocks" for a in stub.audits)

    def test_manifest_missing_return_4(
        self, tl_main, monkeypatch, tmp_path, sample_raw_output_missing_manifest
    ):
        stub = _StubPatch(tl_main, monkeypatch, tmp_path)
        stub.apply(call_claude_result=sample_raw_output_missing_manifest)

        proj_dir = tmp_path / "vault" / "10-项目" / "demo"
        proj_dir.mkdir(parents=True)

        rc = tl_main._run_module_manifest_mode(
            project="demo",
            task="build",
            proj_dir=proj_dir,
            system_prompt=("STATIC", "DYNAMIC"),
            base_prompt="BASE\n",
        )
        assert rc == 4
        # 模块详情被写了（但缺关键产物）
        assert any(p.name == "T01-骨架.md" for p, _ in stub.writes)
        assert not any(p.name == "模块清单.md" for p, _ in stub.writes)
        assert any(a["error"] == "manifest_missing" for a in stub.audits)


class TestUserPromptBuilder:
    """确认 build_module_manifest_user_prompt 拼装的 prompt 含关键约束。"""

    def test_prompt_contains_forbidden_legacy_paths(self, tl_main):
        p = tl_main.build_module_manifest_user_prompt("demo", "BASE\n")
        # legacy 路径明确禁止
        assert "给后端-T0N.md" in p
        assert "给前端-T0N.md" in p
        # 反模式禁止段
        assert "自动化 Python 管道" in p
        # 输出 FILE marker 样板
        assert "<!-- FILE:" in p
        assert "<!-- /FILE -->" in p
        # base_prompt 前置
        assert p.startswith("BASE\n")

    def test_prompt_contains_manifest_schema_hint(self, tl_main):
        p = tl_main.build_module_manifest_user_prompt("demo", "")
        assert "模块清单.md" in p
        assert "nodes:" in p
        assert "backend" in p and "frontend" in p
        assert "拓扑（Mermaid）" in p
