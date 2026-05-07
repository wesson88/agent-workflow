"""
archivist/main.py — 知识沉淀者执行入口（Phase 4g）

作用：
  扫 Claude memory + vault `20-知识/项目记录/`，让 LLM 把够格沉淀的 memory 条目
  晋升为 vault 长期知识笔记，并产出"memory 收紧建议"报告（用户手工执行收紧）。

输入：
  - Claude memory 全部 *.md：~/.claude/projects/<...>/memory/（含 MEMORY.md 索引）
  - vault `20-知识/项目记录/*.md`（去重用）

输出（vault）：
  - 20-知识/项目记录/<topic>.md  0..N 份新晋升
  - 99-临时/memory-shrink-suggestions-{date}.md  收紧建议（用户手工执行）

CLI：
  python .claude/skills/archivist/main.py --task "..." [--memory-dir <path>] [--dry-run]
    --task         本次沉淀说明（必填，写入 audit）
    --memory-dir   memory 目录（默认通过 CLAUDE_MEMORY_DIR 或自动定位本项目 memory）
    --dry-run      只列候选 + 不调 LLM

注意：archivist 不写 Claude memory 本身（用户隐私域），只输出建议报告。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    build_system_prompt, write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now,
)
from engine import (
    set_role_status, role_is_blocked,
    VAULT_ROOT, resolve_path,
)

ROLE = "知识沉淀者"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="知识沉淀者：Claude memory → vault 知识库")
    p.add_argument("--task", required=True, help="沉淀焦点（必填）")
    p.add_argument(
        "--memory-dir", default=None,
        help="memory 目录路径（默认从 CLAUDE_MEMORY_DIR 环境变量读，"
             "再回退到自动定位 ~/.claude/projects/d--<repo-slug>/memory/）",
    )
    p.add_argument("--dry-run", action="store_true", help="只列候选 + 不调 LLM")
    return p.parse_args()


def _autodetect_memory_dir() -> Path | None:
    """自动定位本项目的 Claude memory 目录。

    Claude Code 默认将 memory 存在
      ~/.claude/projects/<sanitized-cwd-path>/memory/
    其中 sanitized 把绝对路径里的 `:` `/` `\\` 都替换成 `-`，前缀有时用 `d--` 表示 D: 盘。
    """
    home = Path.home()
    proj_root = home / ".claude" / "projects"
    if not proj_root.is_dir():
        return None
    cwd_str = str(Path.cwd()).replace(":", "-").replace("\\", "-").replace("/", "-").lower()
    # 候选：以 cwd 路径片段结尾的目录
    candidates = sorted(proj_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in candidates:
        if not d.is_dir():
            continue
        memdir = d / "memory"
        if memdir.is_dir():
            # 简单启发：路径名包含 cwd 末尾片段
            if cwd_str.endswith(d.name.lower()) or d.name.lower() in cwd_str:
                return memdir
    # 找不到精准匹配，但只有一个候选 memory 目录就用它
    found = [d / "memory" for d in candidates if (d / "memory").is_dir()]
    if len(found) == 1:
        return found[0]
    return None


def _gather_memory_files(memory_dir: Path) -> list[Path]:
    """所有 *.md（按 name 排序）。MEMORY.md 索引放最前。"""
    files = sorted(memory_dir.glob("*.md"))
    # MEMORY.md 置顶
    files.sort(key=lambda p: (0 if p.name == "MEMORY.md" else 1, p.name))
    return files


def _gather_existing_vault_knowledge() -> list[Path]:
    """vault `20-知识/项目记录/*.md` 全部，给 LLM 用作去重证据。"""
    knowledge_dir = VAULT_ROOT / "20-知识" / "项目记录"
    if not knowledge_dir.is_dir():
        return []
    return sorted(knowledge_dir.glob("*.md"))


def _read_files_block(files: list[Path], section_title: str) -> str:
    """把一组文件读成 `=== 文件名 ===\n内容\n` 拼接的块。"""
    if not files:
        return f"## {section_title}\n（无）\n"
    parts = [f"## {section_title}（{len(files)} 份）\n"]
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as e:
            content = f"（读取失败：{e}）"
        # 限单文件长度（防爆 prompt）
        if len(content) > 6000:
            content = content[:6000] + f"\n\n…（截断：原文 {len(content)} 字符）"
        parts.append(f"=== {f.name} ===\n{content}\n=== END ===\n")
    return "\n".join(parts)


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def main() -> int:
    args = _parse_args()
    task = (args.task or "").strip()
    dry_run = bool(args.dry_run)
    date_stamp = _today_stamp()

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 1) 定位 memory 目录
    memory_dir_arg = args.memory_dir or os.environ.get("CLAUDE_MEMORY_DIR", "").strip()
    if memory_dir_arg:
        memory_dir = Path(memory_dir_arg).resolve()
    else:
        auto = _autodetect_memory_dir()
        if auto is None:
            print(
                f"[{ROLE}] 未能自动定位 Claude memory 目录。\n"
                f"请用 --memory-dir 显式传入，或设环境变量 CLAUDE_MEMORY_DIR。",
                file=sys.stderr,
            )
            set_role_status(
                ROLE, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
            append_audit({
                "timestamp": utc_now(), "role": ROLE, "project": "*",
                "task": task, "result": "failed", "error": "memory_dir_not_found",
            })
            return 2
        memory_dir = auto

    if not memory_dir.is_dir():
        print(f"[{ROLE}] memory 目录不存在：{memory_dir}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        return 2

    memory_files = _gather_memory_files(memory_dir)
    vault_knowledge = _gather_existing_vault_knowledge()

    print(
        f"[{ROLE}] 输入：memory_dir={memory_dir} ({len(memory_files)} 份) / "
        f"vault `20-知识/项目记录/` ({len(vault_knowledge)} 份已有)"
    )

    if not memory_files:
        print(f"[{ROLE}] memory 目录为空，无可沉淀。")
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        return 0

    if dry_run:
        print(f"[{ROLE}] --dry-run 候选清单：")
        for f in memory_files:
            print(f"  - {f.name} ({f.stat().st_size} bytes)")
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": "*",
            "task": task, "result": "dry_run", "candidate_count": len(memory_files),
        })
        return 0

    # 2) 构造 prompt
    system_prompt = build_system_prompt(ROLE, project=None)

    memory_block = _read_files_block(memory_files, "Claude memory 全部文件")
    vault_block = _read_files_block(vault_knowledge, "vault 已有知识笔记（去重用）")

    suggestions_path = f"99-临时/memory-shrink-suggestions-{date_stamp}.md"

    user_prompt = (
        f"# 沉淀焦点（来自 --task）\n{task}\n\n"
        f"# 输入\n\n{memory_block}\n\n{vault_block}\n\n"
        f"---\n\n"
        f"# 你的任务\n\n"
        f"按角色基因 §3 晋升判定原则逐份评估 Claude memory 文件：\n"
        f"- **应晋升**：架构决策的『为什么』/ 路线图 / 跨项目工程模式 / 用户重要决策与理由\n"
        f"- **应留 memory**：阶段进度 / 单次会话 / Claude 行为微调（除非通用化）\n"
        f"- **已在 vault** 等价版：跳过\n\n"
        f"## 输出格式（强制）\n\n"
        f"必须输出 1 个 FILE 块（收紧建议报告）+ 0..N 个 FILE 块（晋升的 vault 笔记）：\n\n"
        f"<!-- FILE: {suggestions_path} -->\n"
        f"（按角色基因 §4.2 报告结构产出：已晋升 / 保留 / 已存在 vault 三段）\n"
        f"<!-- /FILE -->\n\n"
        f"<!-- FILE: 20-知识/项目记录/<topic-slug>.md -->\n"
        f"（按角色基因 §4.1 笔记结构产出）\n"
        f"<!-- /FILE -->\n\n"
        f"严格筛选：宁可少晋升 0-3 份，不要把日记本式 memory（如阶段进度）误升。"
        f"已存在的 vault 知识笔记不要重复创建（在 `## 已在 vault 找到等价版` 段说明匹配证据）。"
    )

    # 3) 调用 LLM
    try:
        raw = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] LLM 调用失败：{e}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": "*",
            "task": task, "result": "failed", "error": str(e),
        })
        return 1

    # 4) 写盘
    files = parse_claude_output_to_files(raw)
    if not files:
        # 降级：整体写到收紧建议路径
        dest = resolve_path(suggestions_path, project=None)
        write_output_atomic(dest, raw)
        written = [suggestions_path]
        promoted: list[str] = []
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        written = []
        promoted = []
        for rel_path, content in files.items():
            # 安全护栏：拒绝改自己 / 改 00-系统 / 改 10-项目
            if rel_path.endswith("角色-知识沉淀者.md"):
                print(f"[{ROLE}] ⚠️  拒绝改自己：{rel_path}", file=sys.stderr)
                continue
            if rel_path.startswith("00-系统/"):
                print(f"[{ROLE}] ⚠️  拒绝改 00-系统/：{rel_path}（角色基因 / 规则 / 工作流模板专属）", file=sys.stderr)
                continue
            if rel_path.startswith("10-项目/"):
                print(f"[{ROLE}] ⚠️  拒绝改 10-项目/：{rel_path}（项目产出区不归你管）", file=sys.stderr)
                continue
            dest = resolve_path(rel_path, project=None)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_path)
            if rel_path.startswith("20-知识/项目记录/"):
                promoted.append(rel_path)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": "*",
        "task": task, "result": "success",
        "outputs": written,
        "promoted": promoted,
        "memory_files_scanned": len(memory_files),
        "vault_knowledge_existing": len(vault_knowledge),
    })
    print(
        f"\n[{ROLE}] 完成。晋升新建：{len(promoted)} 份；"
        f"收紧建议见：{suggestions_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
