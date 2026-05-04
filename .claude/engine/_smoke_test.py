"""
_smoke_test.py — Phase 2 engine 模块自检。

跑法：
    python .claude/engine/_smoke_test.py

不会改动任何 vault 文件，只读 + 仅在 99-临时/ 写一份临时文件验证 IO 可达。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 gbk，重配 stdout 为 utf-8 以正确显示中文与 emoji
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# 让脚本可以独立运行（不依赖被作为 package 安装）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import (
    VAULT_ROOT, PROJECT_NAME,
    role_genes_dir, rules_dir, project_dir,
    list_roles, load_role, RoleNotFound,
    get_role_status, summarize_all_roles,
    read_note, write_note, append_to_note, update_frontmatter,
    list_notes,
)


def title(s: str) -> None:
    print()
    print("=" * 60)
    print(s)
    print("=" * 60)


def main() -> int:
    title("[1] config 验证")
    print(f"VAULT_ROOT     = {VAULT_ROOT}")
    print(f"PROJECT_NAME   = {PROJECT_NAME}")
    print(f"role_genes_dir = {role_genes_dir()}")
    print(f"rules_dir      = {rules_dir()}")
    print(f"project_dir()  = {project_dir()}")
    assert VAULT_ROOT.is_dir(), f"VAULT_ROOT 不存在：{VAULT_ROOT}"
    assert role_genes_dir().is_dir(), f"角色基因目录不存在：{role_genes_dir()}"
    assert (rules_dir() / "技术栈.md").exists(), "技术栈.md 缺失"
    assert (rules_dir() / "架构分解规则.md").exists(), "架构分解规则.md 缺失"

    title("[2] role_loader：加载所有角色（Role 仅含静态定义）")
    roles = list_roles()
    assert len(roles) == 5, f"期望 5 个角色，实际 {len(roles)}"
    print(f"加载到 {len(roles)} 个角色：")
    for r in roles:
        st = get_role_status(r.name)
        print(
            f"  - [{st['status']:<11}] {r.name} "
            f"(model={r.model}, max_tokens={r.max_tokens})  "
            f"← {','.join(r.upstream) or 'null'} → {','.join(r.downstream) or '[]'}"
        )
        # 确保 Role 不再含运行时字段
        assert not hasattr(r, "status"), f"{r.name}: Role 不应有 status 属性"
        # 确保正文里 DYNAMIC 标记保留
        assert "<!-- DYNAMIC_START -->" in r.body, f"{r.name} 缺少 DYNAMIC_START"
        assert "<!-- DYNAMIC_END -->" in r.body, f"{r.name} 缺少 DYNAMIC_END"

    title("[3] role_loader：按别名查找")
    by_cn = load_role("产品经理")
    by_en = load_role("product_manager")
    by_alias = load_role("PM")
    assert by_cn.note_path == by_en.note_path == by_alias.note_path, \
        "产品经理/product_manager/PM 应解析到同一个笔记"
    print(f"产品经理 ≡ product_manager ≡ PM → {by_cn.note_path.name}")

    by_arch = load_role("chief_architect")
    assert by_arch.name == "架构师"
    arch_status = get_role_status("架构师")["status"]
    print(f"chief_architect → {by_arch.name}（runtime status={arch_status}）")

    title("[4] role_loader：未知名查找应抛 RoleNotFound")
    try:
        load_role("不存在的角色")
        print("❌ 期望抛 RoleNotFound 但没抛")
        return 1
    except RoleNotFound as e:
        print(f"✅ 正确抛出 RoleNotFound：{e}")

    title("[5] state.summarize_all_roles")
    snapshot = summarize_all_roles()
    for s in snapshot:
        print(f"  {s}")

    title("[6] obsidian_io：写入 + 追加 + frontmatter 局部更新（99-临时/_smoke）")
    smoke_path = "99-临时/_smoke_test.md"
    write_note(smoke_path, "---\nrole: 测试\nstatus: idle\n---\n\nbody-1\n")
    append_to_note(smoke_path, "\nbody-2 appended\n")
    update_frontmatter(smoke_path, {"status": "busy", "extra": "added"})

    txt = read_note(smoke_path)
    assert "status: busy" in txt, "frontmatter 更新失败"
    assert "extra: added" in txt, "新字段未写入"
    assert "body-1" in txt and "body-2 appended" in txt, "body 丢失"
    print("✅ 写入/追加/frontmatter 更新均正常")
    print(f"   文件位置：{VAULT_ROOT / smoke_path}")

    title("[7] list_notes：枚举角色基因目录")
    role_notes = list_notes("00-系统/角色基因", "*.md")
    print(f"枚举到 {len(role_notes)} 个角色笔记")
    assert len(role_notes) == 5, f"期望 5 个角色笔记，实际 {len(role_notes)}"

    title("✅ Phase 2 engine 全部模块验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
