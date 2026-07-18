"""
engine/artifact_registry.py — 产物注册表加载器（架构演进第 2 步 v0.1 骨架）

规范：vault `00-系统/规则/产物注册表规范.md`。
设计：[[架构演进方向-角色接口化与跨域组合-2026-07-18]] 缺口 3 + 三决策
（{proj_root} 路径抽象 / 每产物一个 vault 笔记 / 中文 artifact_id）。

v0.1 提供：
- load_registry()：扫 `00-系统/产物注册表/<domain>/<artifact>.md`，fail-closed 校验
- resolve_artifact_path(artifact_id, project)：类型名 → vault 相对路径
- coverage_report()：对照全部角色的 inputs/outputs 声明，审计哪些路径已注册
  /未注册（影子模式 v0.2 的前置全景，只读不改任何角色）

CLI：
  python -m engine.artifact_registry --list
  python -m engine.artifact_registry --validate
  python -m engine.artifact_registry --coverage
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import VAULT_ROOT
from .obsidian_io import read_note, split_frontmatter

_REGISTRY_SUBDIR = ("00-系统", "产物注册表")
_CONFIG_STEM = "_config"
_ALLOWED_FORMATS = frozenset({"md", "json", "dir"})


class ArtifactRegistryError(ValueError):
    """注册表条目 schema 违规 / 配置缺失。fail-closed（与 manifest 系同哲学）。"""


@dataclass(frozen=True)
class ArtifactSpec:
    """一条产物注册（frontmatter 的类型化形态）。"""
    artifact: str                       # 中文 id == 笔记 stem
    domain: str
    path_template: str                  # 含 {proj_root}
    format: str                         # md / json / dir
    producer: str
    consumers: tuple[str, ...] = ()
    schema_ref: str | None = None
    lint: str | None = None
    note_path: Path | None = None

    def resolve(self, proj_roots: dict[str, str], project: str | None = None) -> str:
        """渲染实际路径（vault 相对）。project=None 时保留 {project} 占位符。"""
        root = proj_roots[self.domain]
        path = self.path_template.replace("{proj_root}", root)
        if project is not None:
            path = path.replace("{project}", project)
        return path


def _registry_dir() -> Path:
    return VAULT_ROOT.joinpath(*_REGISTRY_SUBDIR)


def load_config() -> dict[str, str]:
    """读 _config.md 的 proj_roots 映射。缺失 → raise（注册表不可用）。"""
    cfg_path = _registry_dir() / f"{_CONFIG_STEM}.md"
    if not cfg_path.is_file():
        raise ArtifactRegistryError(
            f"产物注册表配置缺失：{cfg_path}（需含 frontmatter.proj_roots）"
        )
    fm, _ = split_frontmatter(read_note(cfg_path))
    proj_roots = fm.get("proj_roots")
    if not isinstance(proj_roots, dict) or not proj_roots:
        raise ArtifactRegistryError(
            f"{cfg_path} frontmatter.proj_roots 缺失或非 mapping"
        )
    return {str(k): str(v) for k, v in proj_roots.items()}


def _build_spec(note: Path, proj_roots: dict[str, str]) -> ArtifactSpec:
    fm, _ = split_frontmatter(read_note(note))
    art = str(fm.get("artifact", "")).strip()
    if not art:
        raise ArtifactRegistryError(f"{note}: 缺 artifact 字段")
    if art != note.stem:
        raise ArtifactRegistryError(
            f"{note}: artifact='{art}' 必须等于笔记 stem '{note.stem}'"
            f"（wikilink 一致性，规范 §2）"
        )
    domain = str(fm.get("domain", "")).strip()
    if domain not in proj_roots:
        raise ArtifactRegistryError(
            f"{note}: domain='{domain}' 未在 _config.proj_roots 声明"
            f"（已声明：{sorted(proj_roots)}）"
        )
    tpl = str(fm.get("path_template", "")).strip()
    if "{proj_root}" not in tpl:
        raise ArtifactRegistryError(
            f"{note}: path_template 必须含 {{proj_root}} 占位符（规范 §3），"
            f"实际：{tpl!r}"
        )
    fmt = str(fm.get("format", "")).strip()
    if fmt not in _ALLOWED_FORMATS:
        raise ArtifactRegistryError(
            f"{note}: format='{fmt}' 非法（允许：{sorted(_ALLOWED_FORMATS)}）"
        )
    producer = str(fm.get("producer", "")).strip()
    if not producer:
        raise ArtifactRegistryError(f"{note}: 缺 producer 字段")
    consumers_raw = fm.get("consumers") or []
    if not isinstance(consumers_raw, list):
        raise ArtifactRegistryError(f"{note}: consumers 必须是 list")
    return ArtifactSpec(
        artifact=art,
        domain=domain,
        path_template=tpl,
        format=fmt,
        producer=producer,
        consumers=tuple(str(c) for c in consumers_raw),
        schema_ref=(str(fm["schema_ref"]) if fm.get("schema_ref") else None),
        lint=(str(fm["lint"]) if fm.get("lint") else None),
        note_path=note,
    )


@lru_cache(maxsize=1)
def _load_registry_cached() -> tuple[dict[str, str], dict[str, ArtifactSpec]]:
    proj_roots = load_config()
    d = _registry_dir()
    registry: dict[str, ArtifactSpec] = {}
    for note in sorted(d.rglob("*.md")):
        if note.stem.startswith("_"):
            continue
        spec = _build_spec(note, proj_roots)
        if spec.artifact in registry:
            raise ArtifactRegistryError(
                f"artifact '{spec.artifact}' 重复注册：{registry[spec.artifact].note_path} "
                f"与 {note}"
            )
        registry[spec.artifact] = spec
    return proj_roots, registry


def invalidate_cache() -> None:
    _load_registry_cached.cache_clear()


def load_registry() -> dict[str, ArtifactSpec]:
    """artifact_id → ArtifactSpec。目录不存在返回空 dict（注册表未启用）。"""
    if not _registry_dir().is_dir():
        return {}
    return _load_registry_cached()[1]


def get_artifact(artifact_id: str) -> ArtifactSpec:
    registry = load_registry()
    if artifact_id not in registry:
        raise KeyError(
            f"未注册的 artifact：'{artifact_id}'。已注册：{sorted(registry)}"
        )
    return registry[artifact_id]


def resolve_artifact_path(artifact_id: str, project: str | None = None) -> str:
    """类型名 → vault 相对路径（project=None 保留 {project} 占位符）。"""
    proj_roots, registry = _load_registry_cached()
    if artifact_id not in registry:
        raise KeyError(
            f"未注册的 artifact：'{artifact_id}'。已注册：{sorted(registry)}"
        )
    return registry[artifact_id].resolve(proj_roots, project)


# ── 覆盖率审计（影子模式 v0.2 前置全景，只读）─────────────
def coverage_report() -> dict:
    """对照全部角色 inputs/outputs 声明与注册表：

    返回 {
      "registered": [(role, path, artifact_id), ...],   # 声明路径命中注册条目
      "unregistered": [(role, path), ...],              # 未命中（含指令/过程/规则类）
      "artifact_count": int,
    }
    匹配 = 角色声明的路径字符串 == 条目模板渲染（保留 {project} 占位符）。
    纯字符串对照，不改任何角色，供 v0.2 迁移排优先级。
    """
    from .role_loader import list_roles

    proj_roots, registry = _load_registry_cached() if _registry_dir().is_dir() else ({}, {})
    resolved = {
        spec.resolve(proj_roots).replace("\\", "/"): aid
        for aid, spec in registry.items()
    }
    registered: list[tuple[str, str, str]] = []
    unregistered: list[tuple[str, str]] = []
    for role in list_roles():
        for path in (*role.inputs, *role.outputs):
            norm = str(path).strip().replace("\\", "/")
            hit = resolved.get(norm)
            if hit:
                registered.append((role.name, norm, hit))
            else:
                unregistered.append((role.name, norm))
    return {
        "registered": registered,
        "unregistered": unregistered,
        "artifact_count": len(registry),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(prog="engine.artifact_registry")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出全部注册产物")
    group.add_argument("--validate", action="store_true", help="fail-closed 校验全部条目")
    group.add_argument("--coverage", action="store_true", help="角色 I/O 声明覆盖率审计")
    args = parser.parse_args(argv)

    if args.list or args.validate:
        try:
            registry = load_registry()
        except ArtifactRegistryError as e:
            print(f"❌ 注册表校验失败：{e}", file=sys.stderr)
            return 2
        if args.validate:
            print(f"✅ 注册表校验通过（{len(registry)} 个产物）")
            return 0
        for aid, spec in sorted(registry.items()):
            print(f"[{spec.domain}] {aid}  →  {spec.path_template}"
                  f"  (producer={spec.producer}, format={spec.format})")
        return 0

    # --coverage
    report = coverage_report()
    reg, unreg = report["registered"], report["unregistered"]
    total = len(reg) + len(unreg)
    print(f"产物注册表覆盖率：{len(reg)}/{total} 条角色 I/O 声明已注册"
          f"（注册产物 {report['artifact_count']} 个）\n")
    print("── 已注册 ──")
    for role, path, aid in reg:
        print(f"  {role}: {path}  →  [[{aid}]]")
    print("\n── 未注册（指令/过程/规则/代码类 + 待补产物）──")
    for role, path in unreg:
        print(f"  {role}: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
