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

# 项目代码根目录模板。支持 {project} 占位符。
# 不设置 → 退回 PROJECT_ROOT（即引擎仓自身，legacy 行为；多项目共用 src/）
# 设置 → 每个项目独立目录，例如 PROJECT_CODE_ROOT=D:/workstation/ai/tools/{project}
#        会让 _pain-point-radar 的代码写到 D:/workstation/ai/tools/_pain-point-radar/
PROJECT_CODE_ROOT_TEMPLATE: str | None = (
    os.environ.get("PROJECT_CODE_ROOT") or None
)

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


def project_code_root(project: str | None = None) -> Path:
    """返回当前项目的代码根目录（src / tests / requirements.txt 等的容器）。

    - 设了 `PROJECT_CODE_ROOT` 环境变量 → 展开 `{project}` 占位符并 `mkdir -p`
    - 未设 → fallback 到 `PROJECT_ROOT`（引擎仓自身；legacy 多项目共用 src/ 行为）
    """
    if PROJECT_CODE_ROOT_TEMPLATE:
        name = (project or PROJECT_NAME).strip() or "default"
        expanded = PROJECT_CODE_ROOT_TEMPLATE.replace("{project}", name)
        path = Path(expanded).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return PROJECT_ROOT


# ── 路径模板解析 ─────────────────────────────────────────
# 角色笔记 / Claude 输出的路径形式：
#   1. "10-项目/{project}/PRD.md"   → vault 内（项目产出）
#   2. "00-系统/规则/技术栈.md"      → vault 内（全局规则）
#   3. "src/backend/main.py"       → 项目仓内（代码）
#   4. "pytest.ini" / "package.json" → 项目仓内（仓根配置文件，无目录前缀）
#
# 判定规则（从严判定 vault 归属，避免裸文件名被误投放到 vault）：
# - 路径以已知 vault 前缀开头 → vault
# - 其余 → 项目仓

_VAULT_PREFIXES = ("00-系统", "10-项目", "20-知识", "99-临时")

# 仓根白名单：必须放在项目仓根的配置文件（被工具自动发现的）。
# 其余无目录前缀的文件名一律重定向到 vault 10-项目/{project}/ 下，
# 避免 dev_backend 等角色输出 README.md/requirements.txt 时覆盖引擎仓自身文件。
_REPO_ROOT_WHITELIST = frozenset({
    "pytest.ini",
    "conftest.py",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    ".gitignore",
    ".env",
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Makefile",
})


def resolve_path(path_template: str, project: str | None = None) -> Path:
    """把角色 frontmatter / Claude 输出的路径模板解析为绝对路径。

    - {project} 占位符替换为传入的 project（缺省用 PROJECT_NAME）
    - vault 路径必须以 `00-系统` / `10-项目` / `20-知识` / `99-临时` 开头
    - 包含目录前缀的项目仓路径（`src/...`、`tests/...`、`static/...`）→ 项目仓
    - 裸文件名（无目录前缀）：白名单内（pytest.ini / conftest.py / pyproject.toml /
      package.json / .env 等）→ 仓根；其它（README.md / requirements.txt 等）
      自动重定向到 vault `10-项目/{project}/<filename>` 并打印 warning。
    - 末尾的 '/' 会被剥离，便于 Path 正常拼接
    """
    import sys

    name = (project or PROJECT_NAME).strip() or "default"
    expanded = path_template.replace("{project}", name).strip().rstrip("/").rstrip("\\")
    if not expanded:
        raise ValueError(f"路径模板为空：{path_template!r}")
    norm = expanded.replace("\\", "/")
    head = norm.split("/", 1)[0]

    if head in _VAULT_PREFIXES:
        return (VAULT_ROOT / expanded).resolve()

    code_root = project_code_root(project)

    # 路径含目录前缀（src/、tests/ 等）→ 项目代码根
    if "/" in norm:
        return (code_root / expanded).resolve()

    # 裸文件名：白名单 → 项目代码根
    if expanded in _REPO_ROOT_WHITELIST:
        return (code_root / expanded).resolve()

    # 裸文件名 + 不在白名单
    # per-project 模式：code_root 已是项目专属目录，直接放进去安全
    if PROJECT_CODE_ROOT_TEMPLATE:
        return (code_root / expanded).resolve()

    # legacy 模式（PROJECT_CODE_ROOT 未设）：避免覆盖引擎仓文件，重定向到 vault
    redirected = f"10-项目/{name}/{expanded}"
    print(
        f"⚠️  路径自动重定向：'{path_template}' → '{redirected}' "
        f"（裸文件名不在仓根白名单 + 未配 PROJECT_CODE_ROOT，避免覆盖引擎仓文件）",
        file=sys.stderr,
    )
    return (VAULT_ROOT / redirected).resolve()
