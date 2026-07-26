"""
music_lyricist/main.py — 作词 CLI 壳（role_runner 收编后 · 架构演进第 3 步）

生产路径不走本文件：角色基因声明 `executor: in_process`，工作流经
invoke_role(mode="auto") 路由到 engine.role_runner 声明驱动流水线；
inputs/outputs/rule_refs/skill 注入全部来自角色 frontmatter + 产物注册表声明。

本壳仅保留单角色 CLI 调试入口，同样经 invoke_role 走 runner——CLI 与生产
单一实现（防两套流水线漂移）。壳内固定 mode="in_process"：不用 "auto"，
因为路由若 fallback 到 subprocess 会重新 spawn 本文件造成递归。

CLI：
  python .claude/skills/music_lyricist/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import parse_args, resolve_project
from engine.role_invoke import RoleInvocation, invoke_role

ROLE = "作词"


def main() -> int:
    args = parse_args()
    inv = RoleInvocation(
        role=ROLE,
        task=(args.task or "").strip(),
        project=resolve_project(args),
    )
    res = invoke_role(inv, mode="in_process")
    if res.status == "success":
        return 0
    if res.error:
        print(f"[{ROLE}] {res.error}", file=sys.stderr)
    # invoke_role 的 -2/-3（runner 异常 / 消费端门禁拦截）折算为永久错误码 2
    return res.returncode if res.returncode > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
