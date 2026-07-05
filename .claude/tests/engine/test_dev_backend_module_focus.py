"""
test_dev_backend_module_focus.py — P8.7 补测：dev_backend / dev_frontend
`_resolve_module_task_files` helper 的 glob 行为锁定。

覆盖点：
- module_id 匹配到多份 `模块/{id}-*.md`（按 name 排序返回）
- module_id 未匹配 → 空 list
- 目录不存在 → 空 list（不抛）
- 前缀不完整（module_id="T1" 不应误命中 "T10-..."）——glob 用 `{id}-*` pattern
- dev_backend 和 dev_frontend 的 helper 行为一致（对称）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 skills 目录在 sys.path 里，import dev_backend / dev_frontend 的 main.py
_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))
sys.path.insert(0, str(_SKILLS_DIR / "dev_backend"))
sys.path.insert(0, str(_SKILLS_DIR / "dev_frontend"))


def _import_dev_backend_helper():
    """从 dev_backend/main.py 拉 helper（避免污染 pytest collect）。"""
    import importlib.util
    path = _SKILLS_DIR / "dev_backend" / "main.py"
    spec = importlib.util.spec_from_file_location("dev_backend_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._resolve_module_task_files


def _import_dev_frontend_helper():
    import importlib.util
    path = _SKILLS_DIR / "dev_frontend" / "main.py"
    spec = importlib.util.spec_from_file_location("dev_frontend_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._resolve_module_task_files


@pytest.fixture
def proj_dir(tmp_path: Path) -> Path:
    """构造 tmp 项目目录，含 `模块/` 子目录 + 若干模块详情文件。"""
    p = tmp_path / "demo"
    (p / "模块").mkdir(parents=True)
    return p


def _write(proj_dir: Path, name: str, body: str = "# stub\n") -> None:
    (proj_dir / "模块" / name).write_text(body, encoding="utf-8")


class TestResolveModuleTaskFilesBackend:
    def test_single_match_returns_one_file(self, proj_dir):
        _write(proj_dir, "T01-项目脚手架.md")
        _resolve = _import_dev_backend_helper()
        result = _resolve(proj_dir, "T01")
        assert len(result) == 1
        assert result[0].name == "T01-项目脚手架.md"

    def test_no_match_returns_empty(self, proj_dir):
        _write(proj_dir, "T01-脚手架.md")
        _resolve = _import_dev_backend_helper()
        assert _resolve(proj_dir, "T99") == []

    def test_missing_dir_returns_empty(self, tmp_path):
        # 项目目录下没有 `模块/` 子目录 → glob 空、不抛
        _resolve = _import_dev_backend_helper()
        assert _resolve(tmp_path / "no_such_project", "T01") == []

    def test_multi_match_sorted_by_name(self, proj_dir):
        # 极端场景：手动写多份 T01-*.md（正常不该发生），验 sorted 行为
        _write(proj_dir, "T01-b.md")
        _write(proj_dir, "T01-a.md")
        _resolve = _import_dev_backend_helper()
        result = _resolve(proj_dir, "T01")
        assert [p.name for p in result] == ["T01-a.md", "T01-b.md"]

    def test_prefix_boundary_T1_does_not_match_T10(self, proj_dir):
        """module_id="T1" 不应误命中 "T10-..."（依赖 glob `{id}-*` 而不是 `{id}*`）。"""
        _write(proj_dir, "T10-数据模型.md")
        _write(proj_dir, "T1-数据模型.md")
        _resolve = _import_dev_backend_helper()
        result = _resolve(proj_dir, "T1")
        assert [p.name for p in result] == ["T1-数据模型.md"]

    def test_dash_required_no_bare_id_match(self, proj_dir):
        """无 `-` 分隔的裸文件名 `T01.md` 不命中（pattern 要求 `T01-*`）。"""
        _write(proj_dir, "T01.md")
        _resolve = _import_dev_backend_helper()
        assert _resolve(proj_dir, "T01") == []


class TestResolveModuleTaskFilesFrontend:
    """dev_frontend 的 helper 与 dev_backend 完全对称（同款 glob）。"""

    def test_frontend_helper_same_behavior_as_backend(self, proj_dir):
        _write(proj_dir, "T02-登录页.md")
        backend = _import_dev_backend_helper()
        frontend = _import_dev_frontend_helper()
        assert [p.name for p in backend(proj_dir, "T02")] == [
            p.name for p in frontend(proj_dir, "T02")
        ]

    def test_frontend_no_match_empty(self, proj_dir):
        frontend = _import_dev_frontend_helper()
        assert frontend(proj_dir, "T99") == []
