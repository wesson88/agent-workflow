"""
canvas_export/prototype.py — Phase 5a-1/5a-2 离线 CLI

读已有的脑暴 .md → 生成 .canvas（Obsidian Canvas 视图）。

主要逻辑已抽取到 `engine/canvas_export.py`，本脚本是薄壳 CLI：
- 运行时（discussion.py 跑完）会同位写 .canvas（5a-2 已接入）
- 本脚本仅用于离线补救：已有 .md 但无对应 .canvas 时手动生成

CLI：
  python .claude/skills/canvas_export/prototype.py \
      --input  D:/MarkDown/memory/adam/10-项目/_visitor-counter/脑暴-架构评审.md \
      --output D:/MarkDown/memory/adam/99-临时/canvas-prototype-grid.canvas \
      --layout grid          # 或 swimlane
      --no-edges             # 完全不画时序边
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from engine.canvas_export import build_canvas_from_md, write_canvas_atomic


def main() -> int:
    ap = argparse.ArgumentParser(description="脑暴-*.md → .canvas（离线 CLI）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--layout", choices=["grid", "swimlane"], default="grid")
    ap.add_argument("--no-edges", action="store_true", help="不画时序边")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"输入文件不存在：{src}", file=sys.stderr)
        return 1

    md_text = src.read_text(encoding="utf-8")
    canvas = build_canvas_from_md(
        md_text, layout=args.layout, draw_edges=not args.no_edges,
    )

    out = Path(args.output)
    write_canvas_atomic(canvas, out)
    print(
        f"[canvas-cli] {src.name} → {out.name}（{len(canvas['nodes'])} 节点 / "
        f"{len(canvas['edges'])} 边 / layout={args.layout}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
