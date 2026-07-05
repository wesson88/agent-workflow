"""
test_role_loader_capability_refs.py — P10 role_loader `capability_refs` 加载
+ P10.5 A4 fail-closed 校验。

覆盖：
- frontmatter 有 capability_refs → Role.capability_refs 为对应 tuple（前提：manifest 存在）
- frontmatter 无 → 空 tuple
- 类型：str / list[str] / None 都归一化
- 向下兼容：已契约化角色（P4/P7）加载不受影响
- **A4 fail-closed**：capability_refs 引用 root 缺 manifest → `CapabilityRefError`
- **A4 fail-closed**：wikilink 格式非法（无 root）→ `CapabilityRefError`
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from engine.role_loader import CapabilityRefError, _build_role


def _write_fake_manifest(vault_root: Path, root: str) -> None:
    """辅助：在 tmp vault 建 fake manifest 让 A4 fail-closed 校验通过。"""
    p = vault_root / "20-知识" / "能力注册表" / root / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"id": "' + root + '/x", "version": "0.1.0"}', encoding="utf-8")


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
        _write_fake_manifest(tmp_path, "huashu-design")
        path = _write_role_md(
            tmp_path,
            frontmatter_extra='capability_refs: "[[huashu-design/manifest]]"',
        )
        role = _build_role(path)
        assert role.capability_refs == ("[[huashu-design/manifest]]",)

    def test_list_capability_refs(self, tmp_path):
        _write_fake_manifest(tmp_path, "huashu-design")
        _write_fake_manifest(tmp_path, "web-scraper")
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


class TestCapabilityRefsFailClosed:
    """P10.5 A4 fail-closed 校验：capability_refs 引用错误 root 或非法格式 → raise。"""

    def test_missing_manifest_raises(self, tmp_path):
        # 不建 fake manifest → 加载时 raise
        path = _write_role_md(
            tmp_path,
            frontmatter_extra='capability_refs: "[[nonexistent-cap/manifest]]"',
        )
        with pytest.raises(CapabilityRefError, match="nonexistent-cap"):
            _build_role(path)

    def test_one_missing_one_present_still_raises(self, tmp_path):
        # 只有 huashu-design 有 manifest；web-scraper 缺 → 全 fail
        _write_fake_manifest(tmp_path, "huashu-design")
        path = _write_role_md(
            tmp_path,
            frontmatter_extra=(
                "capability_refs:\n"
                '  - "[[huashu-design/manifest]]"\n'
                '  - "[[web-scraper/manifest]]"\n'
            ),
        )
        with pytest.raises(CapabilityRefError, match="web-scraper"):
            _build_role(path)

    def test_invalid_ref_format_raises(self, tmp_path):
        # 格式非法（root 含大写 / 空格 / 空）→ raise
        path = _write_role_md(
            tmp_path,
            frontmatter_extra='capability_refs: "[[Not Valid Format]]"',
        )
        with pytest.raises(CapabilityRefError, match="无法从"):
            _build_role(path)

    def test_error_message_contains_expected_path(self, tmp_path):
        path = _write_role_md(
            tmp_path,
            frontmatter_extra='capability_refs: "[[missing-cap/manifest]]"',
        )
        with pytest.raises(CapabilityRefError) as exc_info:
            _build_role(path)
        # 错误信息含预期 manifest 路径，帮助 debug
        assert "manifest.json" in str(exc_info.value)
        assert "missing-cap" in str(exc_info.value)


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
