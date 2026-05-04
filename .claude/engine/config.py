"""
config.py — 环境变量与路径配置。

启动时自动加载仓根的 .env（若存在），把里面的 KEY=VALUE 注入 os.environ。
然后导出常量供其它模块使用。

约定：
- VAULT_ROOT 必填（缺失时 import 即抛错，配合 .env.example 的清晰提示）
- PROJECT_NAME 默认 'default'，可由 --project CLI 或 PROJECT/PROJECT_NAME 环境变量覆盖
"""

from __future__ import annotations

import os
from pathlib import Path


# ── 项目仓与 .env 路径 ─────────────────────────────────────
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent  # .../agent-workflow/
_ENV_FILE = PROJECT_ROOT / ".env"


def _load_dotenv(env_file: Path) -> None:
    """读取 .env，把未在 os.environ 中设置的 KEY=VALUE 写入。

    解析规则（够用即可，不做完整 POSIX 兼容）：
    - 跳过空行、# 注释、export 前缀
    - 支持 KEY=VALUE，VALUE 不剥离引号外层（如 "abc" / 'abc'）
    - 已存在的环境变量不覆盖（外部传入优先）
    """
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # 剥离一对包裹引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(_ENV_FILE)


# ── 必填项：VAULT_ROOT ───────────────────────────────────
_vault_root_raw = os.environ.get("VAULT_ROOT", "").strip()
if not _vault_root_raw:
    raise RuntimeError(
        f"VAULT_ROOT 未设置。请在 {_ENV_FILE} 中配置 VAULT_ROOT=...\n"
        f"参考模板：{PROJECT_ROOT / '.env.example'}"
    )
VAULT_ROOT: Path = Path(_vault_root_raw).resolve()
if not VAULT_ROOT.is_dir():
    raise RuntimeError(
        f"VAULT_ROOT 指向的目录不存在：{VAULT_ROOT}"
    )


# ── 选填项 ────────────────────────────────────────────────
PROJECT_NAME: str = (
    os.environ.get("PROJECT")
    or os.environ.get("PROJECT_NAME")
    or "default"
).strip()

ANTHROPIC_API_KEY: str | None = os.environ.get("ANTHROPIC_API_KEY") or None

OBSIDIAN_REST_API_URL: str = os.environ.get(
    "OBSIDIAN_REST_API_URL", "http://127.0.0.1:27123"
)
OBSIDIAN_REST_API_TOKEN: str | None = (
    os.environ.get("OBSIDIAN_REST_API_TOKEN") or None
)


# ── vault 内常用路径快捷方式 ──────────────────────────────
def project_dir(project: str | None = None) -> Path:
    """返回 10-项目/{project}/ 的绝对路径（不保证存在）。"""
    name = (project or PROJECT_NAME).strip() or "default"
    return VAULT_ROOT / "10-项目" / name


def role_genes_dir() -> Path:
    return VAULT_ROOT / "00-系统" / "角色基因"


def rules_dir() -> Path:
    return VAULT_ROOT / "00-系统" / "规则"


def reflection_dir() -> Path:
    """复盘记录（替代旧的 audit.jsonl）"""
    return VAULT_ROOT / "00-系统" / "复盘记录"


def workflow_template_dir() -> Path:
    return VAULT_ROOT / "00-系统" / "工作流模板"


# ── 路径模板解析 ─────────────────────────────────────────
# 角色笔记的 inputs/outputs 字段使用以下三种路径形式之一：
#   1. "10-项目/{project}/PRD.md"   → vault 内（最常见）
#   2. "00-系统/规则/技术栈.md"      → vault 内（全局规则）
#   3. "src/backend/" / "tests/"   → 项目仓内（代码产出，不进 vault）
# resolve_path 自动识别归属并返回绝对路径。

_REPO_RELATIVE_PREFIXES = ("src/", "tests/", "dist/", "src\\", "tests\\", "dist\\")


def resolve_path(path_template: str, project: str | None = None) -> Path:
    """把角色 frontmatter 里的路径模板解析为绝对路径。

    - {project} 占位符替换为传入的 project（缺省用 PROJECT_NAME）
    - 以 src/ tests/ dist/ 开头 → 项目仓相对路径（PROJECT_ROOT / ...）
    - 其余 → vault 相对路径（VAULT_ROOT / ...）
    - 末尾的 '/' 会被剥离，便于 Path 正常拼接
    """
    name = (project or PROJECT_NAME).strip() or "default"
    expanded = path_template.replace("{project}", name).strip().rstrip("/").rstrip("\\")
    if not expanded:
        raise ValueError(f"路径模板为空：{path_template!r}")
    norm = expanded.replace("\\", "/")
    if any(norm.startswith(p.rstrip("/")) for p in _REPO_RELATIVE_PREFIXES):
        return (PROJECT_ROOT / expanded).resolve()
    return (VAULT_ROOT / expanded).resolve()
