"""
.claude/engine — Obsidian-backed orchestration engine.

Phase 2 模块清单：
- config:        加载 .env，暴露 VAULT_ROOT、PROJECT、API key 等
- obsidian_io:   vault 文件读写（filesystem-only；REST API 留待后续）
- role_loader:   解析 00-系统/角色基因/ 下的角色笔记，返回 Role 数据类
- state:         汇总角色 frontmatter 的 status 字段（替代 status.json）
- git_sync:      agent 分支约定 + commit/push + PR（gh 可用时）

公开 API 通过本文件 re-export，下游 import 形如：
    from engine import load_role, read_note, ensure_on_agent_branch
"""

from .config import (
    VAULT_ROOT, PROJECT_ROOT, PROJECT_NAME, ANTHROPIC_API_KEY,
    project_dir, role_genes_dir, rules_dir, reflection_dir,
    workflow_template_dir, resolve_path,
)
from .obsidian_io import (
    read_note, write_note, append_to_note,
    update_frontmatter, list_notes,
)
from .role_loader import (
    Role, load_role, list_roles, RoleNotFound,
)
from .state import (
    get_role_status, set_role_status, role_is_blocked,
    summarize_all_roles,
)
from . import runtime_state
from .git_sync import (
    ensure_on_agent_branch, commit_and_push,
    open_or_update_pr, sync_after_run,
)
from .workflow import (
    WorkflowTemplate, WorkflowStep,
    load_workflow, list_workflows, role_to_skill_dir,
)
from .llm import (
    call_llm, call_claude as llm_call_claude,
    get_provider, list_providers, is_provider_available,
    reload_providers, is_api_available, is_cli_available,
)

__all__ = [
    # config
    "VAULT_ROOT", "PROJECT_ROOT", "PROJECT_NAME", "ANTHROPIC_API_KEY",
    "project_dir", "role_genes_dir", "rules_dir", "reflection_dir",
    "workflow_template_dir", "resolve_path",
    # obsidian_io
    "read_note", "write_note", "append_to_note",
    "update_frontmatter", "list_notes",
    # role_loader
    "Role", "load_role", "list_roles", "RoleNotFound",
    # state + runtime_state
    "get_role_status", "set_role_status", "role_is_blocked",
    "summarize_all_roles", "runtime_state",
    # git_sync
    "ensure_on_agent_branch", "commit_and_push",
    "open_or_update_pr", "sync_after_run",
    # workflow
    "WorkflowTemplate", "WorkflowStep",
    "load_workflow", "list_workflows", "role_to_skill_dir",
    # llm
    "call_llm", "llm_call_claude",
    "get_provider", "list_providers", "is_provider_available",
    "reload_providers", "is_api_available", "is_cli_available",
]
