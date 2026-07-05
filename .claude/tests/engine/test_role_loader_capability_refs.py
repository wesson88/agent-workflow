"""
test_role_loader_capability_refs.py — P10 role_loader `capability_refs` 加载。

覆盖：
- frontmatter 有 capability_refs → Role.capability_refs 为对应 tuple
- frontmatter 无 → 空 tuple
- 类型：str / list[str] / None 都归一化
- 向下兼容：已契约化角色（P4/P7）加载不受影响（fixture 用 fake role md）
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from engine.role_loader import _build_role


@pytest.fixture(autouse=True)
def patch_vault_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """monkeypatch VAULT_ROOT 到 tmp_path 让 _build_role 里的 obsidian_io._resolve 通过。"""
    from engine import config as engine_config
    from engine import obsidian_io as obs_io
    from engine import role_loader as rl
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(obs_io, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(rl, "VAULT_ROOT", tmp_path)


def _write_role_md(tmp_path: Path, frontmatter_extra: str = "", body: str = "# fake\n\n## 1. x\n") -> Path:
    parts = [
        "---",
        "role: 测试角色",
        "domain: se",
        "model: claude-sonnet-4-6",
        "max_tokens: 4096",
        "inputs: []",
        "outputs: []",
    ]
    if frontmatter_extra:
        parts.append(frontmatter_extra.rstrip())
    parts.append("---")
    parts.append("")
    parts.append(body)
    p = tmp_path / "角色-测试.md"
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


class TestCapabilityRefsField:
    def test_no_capability_refs_is_empty_tuple(self, tmp_path):
        path = _write_role_md(tmp_path)
        role = _build_role(path)
        assert role.capability_refs == ()

    def test_single_capability_ref_as_string(self, tmp_path):
        path = _write_role_md(
            tmp_path,
            frontmatter_extra='capability_refs: "[[huashu-design/manifest]]"',
        )
        role = _build_role(path)
        assert role.capability_refs == ("[[huashu-design/manifest]]",)

    def test_list_capability_refs(self, tmp_path):
        path = _write_role_md(
            tmp_path,
            frontmatter_extra=(
                "capability_refs:\n"
                '  - "[[huashu-design/manifest]]"\n'
                '  - "[[web-scraper/manifest]]"\n'
            ),
        )
        role = _build_role(path)
        assert role.capability_refs == (
            "[[huashu-design/manifest]]",
            "[[web-scraper/manifest]]",
        )

    def test_null_capability_refs_ok(self, tmp_path):
        path = _write_role_md(
            tmp_path,
            frontmatter_extra="capability_refs: null",
        )
        role = _build_role(path)
        assert role.capability_refs == ()


class TestBackwardCompat:
    """P4/P7 已契约化角色加载不受 capability_refs 加字段影响。"""

    def test_role_without_capability_refs_still_loads(self, tmp_path):
        # 无 capability_refs field，其它 P7 契约化字段完整
        path = _write_role_md(
            tmp_path,
            frontmatter_extra=(
                "input_contract:\n"
                "  parameterizable: true\n"
                "  fields:\n"
                "    task_source:\n"
                "      type: enum\n"
                "      values: [legacy_directives]\n"
                "      default: legacy_directives\n"
                "  templates:\n"
                "    legacy_directives:\n"
                "      inputs: ['10-项目/{project}/x.md']\n"
            ),
            body="# fake\n\n## 1. x\n",
        )
        # 用契约展开的 inputs（避免 _assert_contract_matches_declared 因 fixture inputs=[] 冲突）
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "inputs: []", "inputs: ['10-项目/{project}/x.md']"
            ),
            encoding="utf-8",
        )
        role = _build_role(path)
        assert role.capability_refs == ()
        assert role.resolved_input_contract is not None
