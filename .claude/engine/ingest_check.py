"""
engine/ingest_check.py — 第三方 skill 入库 stem 冲突预检（2026-07-18）

背景：vault wikilink 契约要求 stem 全局唯一；第三方 skill 靠 ingest 前缀
（`ui_` 等）防撞是纯人工约定，撞名只在角色引用时抛 DuplicateStemError
（运行时事后爆炸）。本模块把爆炸点前移到入库时。

用法（CLI）：
  # 入库前预检：候选文件（或裸 stem）能否安全落盘
  python -m engine.ingest_check --candidate "ui_Design Tokens.md"
  python -m engine.ingest_check --candidate D:/downloads/some-skill.md

  # 全 vault 存量扫描：列出所有已存在的 stem 碰撞（本该 DuplicateStemError 的雷）
  python -m engine.ingest_check --scan

exit code：
  0 → 无冲突
  2 → 有冲突（stderr 列出冲突路径）

程序化入口（skillmind ingest 管线用）：
  from engine.ingest_check import check_stem_conflict, scan_vault_collisions
  conflicts = check_stem_conflict("ui_Design Tokens")   # list[Path]，空 = 安全

设计要点：
- 复用 engine.wikilink._stem_index —— 预检视角与解析器视角**严格一致**
  （同样排除 10-项目/99-临时/runtime-state/跨域 adapter；索引里看不到的
  文件本来也不参与 stem 解析，不构成冲突）。
- 预检前 invalidate_cache()，保证读到最新盘面（入库是低频操作，重扫可接受）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .wikilink import _stem_index, invalidate_cache


def check_stem_conflict(candidate: str | Path) -> list[Path]:
    """返回与候选 stem 冲突的已有文件列表；空列表 = 可安全入库。

    candidate 接受：裸 stem（"ui_Design Tokens"）、文件名（含 .md）、
    或任意路径（只取 stem）。
    """
    stem = Path(candidate).stem
    if not stem:
        raise ValueError(f"候选名为空：{candidate!r}")
    invalidate_cache()
    idx = _stem_index()
    return list(idx.get(stem, []))


def scan_vault_collisions() -> dict[str, list[Path]]:
    """全 vault 存量扫描：返回 {stem: [路径...]}，仅含 ≥2 路径的碰撞项。

    这些 stem 一旦被 bare-stem wikilink 引用即抛 DuplicateStemError。
    """
    invalidate_cache()
    idx = _stem_index()
    return {stem: paths for stem, paths in idx.items() if len(paths) >= 2}


def main(argv: list[str] | None = None) -> int:
    # Windows utf-8 保底
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="engine.ingest_check",
        description="第三方 skill 入库 stem 冲突预检 / 全 vault 存量碰撞扫描",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--candidate", action="append", default=None,
        help="候选文件名/路径/裸 stem；可多次指定批量预检",
    )
    group.add_argument(
        "--scan", action="store_true",
        help="全 vault 存量扫描，列出所有 stem 碰撞",
    )
    args = parser.parse_args(argv)

    if args.scan:
        collisions = scan_vault_collisions()
        if not collisions:
            print("✅ 全 vault 无 stem 碰撞")
            return 0
        print(f"❌ 发现 {len(collisions)} 个 stem 碰撞：", file=sys.stderr)
        for stem, paths in sorted(collisions.items()):
            print(f"  stem '{stem}':", file=sys.stderr)
            for p in paths:
                print(f"    - {p}", file=sys.stderr)
        return 2

    rc = 0
    for cand in args.candidate:
        conflicts = check_stem_conflict(cand)
        if conflicts:
            rc = 2
            print(f"❌ '{cand}' stem 冲突（{len(conflicts)} 个已有文件）：", file=sys.stderr)
            for p in conflicts:
                print(f"    - {p}", file=sys.stderr)
            print(
                "  建议：改名（加来源前缀，如 `ui_<source>_<name>`）后再入库。",
                file=sys.stderr,
            )
        else:
            print(f"✅ '{cand}' 无冲突，可入库")
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
