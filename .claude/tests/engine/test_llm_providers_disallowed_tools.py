# 锁定 llm_providers.yaml Claude CLI --disallowed-tools 治理契约。
# 背景：huashu-demo T06 工程师子进程用 mcpvault 写工具绕过 FILE 块沙箱
# 直接落盘 vault（侧写事件），2026-07-25 决策"封不纳管"。
# 详见 vault [[后端API契约使命行为漂移-2026-07-19]]。
from pathlib import Path

import yaml

_PROVIDERS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "engine" / "llm_providers.yaml"
)

# mcpvault 写类工具：必须全部封禁（产出单通道，vault 写只走 FILE 块沙箱）
MCPVAULT_WRITE_TOOLS = {
    "mcp__mcpvault__write_note",
    "mcp__mcpvault__patch_note",
    "mcp__mcpvault__update_frontmatter",
    "mcp__mcpvault__manage_tags",
    "mcp__mcpvault__move_note",
    "mcp__mcpvault__move_file",
    "mcp__mcpvault__delete_note",
}

# mcpvault 读类工具：必须保持放行（能力注入/知识检索合法依赖）
MCPVAULT_READ_TOOLS = {
    "mcp__mcpvault__read_note",
    "mcp__mcpvault__read_multiple_notes",
    "mcp__mcpvault__search_notes",
    "mcp__mcpvault__list_directory",
    "mcp__mcpvault__get_frontmatter",
    "mcp__mcpvault__get_notes_info",
    "mcp__mcpvault__get_vault_stats",
    "mcp__mcpvault__list_all_tags",
    "mcp__mcpvault__wiki_link",
}

# 2026-05-24 归集 review P2：会话/委派类工具补全。
# SlashCommand / McpInputContext 已非现行 CLI 工具名（2026-07-25 实测
# deny 规则报 unknown 警告），不列入。
SESSION_TOOLS = {"Skill", "SendMessage", "KillShell"}

# FILE 块沙箱时代就封禁的基线（文件/网络/任务工具）
BASELINE_TOOLS = {
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "WebFetch", "WebSearch", "TodoWrite", "TaskCreate", "TaskUpdate",
}


def _claude_cli_models() -> dict[str, list[str]]:
    """返回 {model_key: extra_args}，仅含 cli.path == 'claude' 的 provider。"""
    providers = yaml.safe_load(_PROVIDERS_FILE.read_text(encoding="utf-8"))
    return {
        key: cfg["cli"]["extra_args"]
        for key, cfg in providers.items()
        if isinstance(cfg, dict) and cfg.get("cli", {}).get("path") == "claude"
    }


def _disallowed_set(extra_args: list[str]) -> set[str]:
    idx = extra_args.index("--disallowed-tools")
    return set(extra_args[idx + 1].split(","))


def test_all_claude_cli_models_declare_disallowed_tools():
    models = _claude_cli_models()
    assert models, "llm_providers.yaml 里找不到任何 claude CLI provider"
    for key, extra_args in models.items():
        assert "--disallowed-tools" in extra_args, f"{key} 缺 --disallowed-tools"
        assert _disallowed_set(extra_args), f"{key} 的 disallowed 清单为空"


def test_disallowed_list_identical_across_claude_models():
    """anchor 共用单一来源；任何 model 单独漂移都视为治理事故。"""
    models = _claude_cli_models()
    lists = {key: _disallowed_set(args) for key, args in models.items()}
    baseline_key = next(iter(lists))
    for key, tools in lists.items():
        assert tools == lists[baseline_key], (
            f"{key} 与 {baseline_key} 的 disallowed 清单不一致"
        )


def test_mcpvault_write_tools_all_blocked():
    for key, extra_args in _claude_cli_models().items():
        missing = MCPVAULT_WRITE_TOOLS - _disallowed_set(extra_args)
        assert not missing, f"{key} 漏封 mcpvault 写工具: {sorted(missing)}"


def test_mcpvault_read_tools_not_blocked():
    for key, extra_args in _claude_cli_models().items():
        blocked = MCPVAULT_READ_TOOLS & _disallowed_set(extra_args)
        assert not blocked, f"{key} 误封 mcpvault 读工具: {sorted(blocked)}"


def test_session_and_baseline_tools_blocked():
    for key, extra_args in _claude_cli_models().items():
        disallowed = _disallowed_set(extra_args)
        assert SESSION_TOOLS <= disallowed, (
            f"{key} 缺会话类工具: {sorted(SESSION_TOOLS - disallowed)}"
        )
        assert BASELINE_TOOLS <= disallowed, (
            f"{key} 缺基线工具: {sorted(BASELINE_TOOLS - disallowed)}"
        )
