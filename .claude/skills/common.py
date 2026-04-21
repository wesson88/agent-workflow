"""
common.py - 技能共享工具库
所有 main.py 通过 sys.path.insert(0, str(Path(__file__).resolve().parent)) import 此模块
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile

import anthropic

# ----------------------------------------------------------------
# 路径管理
# ----------------------------------------------------------------

def get_claude_root() -> Path:
    """返回 workflow/.claude/ 目录（common.py 的父目录的父目录）"""
    return Path(__file__).resolve().parent.parent

def get_project_root() -> Path:
    """返回 workflow/ 目录"""
    return get_claude_root().parent

def get_skill_dir(skill_name: str) -> Path:
    return get_claude_root() / "skills" / skill_name

def get_skill_md(skill_name: str) -> Path:
    return get_skill_dir(skill_name) / "skill.md"

def get_status_path() -> Path:
    return get_claude_root() / "status.json"

def get_audit_path() -> Path:
    return get_claude_root() / "audit.jsonl"

def get_instructions_dir() -> Path:
    return get_claude_root() / "instructions"

def get_docs_dir() -> Path:
    return get_claude_root() / "docs"

def get_requirements_dir() -> Path:
    return get_claude_root() / "requirements"

def get_inputs_dir() -> Path:
    return get_claude_root() / "inputs"

def get_src_dir() -> Path:
    return get_project_root() / "src"

# ----------------------------------------------------------------
# skill.md 读取与动态区域处理
# ----------------------------------------------------------------

DYNAMIC_START = "<!-- DYNAMIC_START -->"
DYNAMIC_END = "<!-- DYNAMIC_END -->"

OUTPUT_FORMAT_SPEC = """
## 输出格式规范（强制遵守）
当你需要写入文件时，使用以下标签格式包裹每个文件的内容：

<!-- FILE: 相对路径/文件名.ext -->
文件内容
<!-- /FILE -->

- 路径相对于项目根目录（workflow/）
- 一次响应中可包含多个 FILE 块
- 文件路径不得包含空格
- 代码文件不需要额外的 Markdown 代码块包裹
"""


def read_skill_md(skill_name: str) -> str:
    path = get_skill_md(skill_name)
    if not path.exists():
        raise FileNotFoundError(f"skill.md 不存在: {path}")
    return path.read_text(encoding="utf-8")


def extract_dynamic_patch(skill_md_content: str) -> str:
    """提取 DYNAMIC 区域中的有效补丁指令（过滤纯注释行）"""
    pattern = re.compile(
        re.escape(DYNAMIC_START) + r"(.*?)" + re.escape(DYNAMIC_END),
        re.DOTALL
    )
    match = pattern.search(skill_md_content)
    if not match:
        return ""
    patch = match.group(1).strip()
    lines = [l for l in patch.splitlines() if l.strip() and not l.strip().startswith("#")]
    return "\n".join(lines).strip()


def build_system_prompt(skill_name: str, upstream_skill: str = None) -> str:
    """
    构造 system prompt：
    1. 读取本技能的 skill.md（已包含自身 DYNAMIC 区域）
    2. 若指定 upstream_skill，读取其 DYNAMIC 区域并追加
    3. 追加输出格式规范
    """
    base = read_skill_md(skill_name)
    parts = [base]

    if upstream_skill:
        try:
            upstream_content = read_skill_md(upstream_skill)
            patch = extract_dynamic_patch(upstream_content)
            if patch:
                parts.append(f"\n\n## 上游技能 [{upstream_skill}] 动态补丁指令\n{patch}")
        except FileNotFoundError:
            pass

    parts.append(OUTPUT_FORMAT_SPEC)
    return "\n".join(parts)

# ----------------------------------------------------------------
# 文件读取工具
# ----------------------------------------------------------------

def read_file_safe(path: Path, default: str = "") -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return default


def read_input_files(file_paths: list) -> str:
    """合并多个输入文件为上下文块"""
    parts = []
    for fp in file_paths:
        fp = Path(fp)
        content = read_file_safe(fp)
        if content:
            parts.append(f"=== {fp.name} ===\n{content}\n===")
        else:
            parts.append(f"=== {fp.name} ===\n（文件不存在或为空）\n===")
    return "\n\n".join(parts)

# ----------------------------------------------------------------
# 输出文件写入
# ----------------------------------------------------------------

def write_output_atomic(dest_path: Path, content: str):
    """原子写入文件"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=dest_path.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp"
    ) as tf:
        tf.write(content)
        tmp = tf.name
    os.replace(tmp, dest_path)


def parse_claude_output_to_files(raw_output: str) -> dict:
    """
    解析 Claude 输出中的多文件块。
    格式：<!-- FILE: path/to/file.ext -->\n内容\n<!-- /FILE -->
    返回：{相对路径: 内容}
    """
    pattern = re.compile(
        r"<!--\s*FILE:\s*(.+?)\s*-->\n(.*?)<!--\s*/FILE\s*-->",
        re.DOTALL
    )
    results = {}
    for match in pattern.finditer(raw_output):
        rel_path = match.group(1).strip()
        content = match.group(2)
        results[rel_path] = content
    return results

# ----------------------------------------------------------------
# status.json 操作
# ----------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_status() -> dict:
    path = get_status_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"skill_registry": {}}


def save_status(status: dict):
    path = get_status_path()
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tf:
        json.dump(status, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    os.replace(tmp, path)


def update_skill_status(skill_name: str, updates: dict):
    status = load_status()
    registry = status.setdefault("skill_registry", {})
    skill = registry.setdefault(skill_name, {})
    skill.update(updates)
    skill["last_run"] = utc_now()
    save_status(status)

# ----------------------------------------------------------------
# 审计日志
# ----------------------------------------------------------------

def append_audit(entry: dict):
    path = get_audit_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# ----------------------------------------------------------------
# CLI 参数解析
# ----------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, help="任务描述")
    parser.add_argument("--sub-skill", default=None, dest="sub_skill", help="子技能名称（可选）")
    return parser.parse_args()

# ----------------------------------------------------------------
# Claude API 调用
# ----------------------------------------------------------------

MAX_TOKENS_MAP = {
    "chief_architect": 4096,
    "technical_lead": 4096,
    "dev_backend": 8192,
    "dev_frontend": 8192,
}


def call_claude(system_prompt: str, user_prompt: str, skill_name: str) -> str:
    """
    Streaming 调用 Claude，实时打印输出并返回完整响应字符串。
    从环境变量 ANTHROPIC_API_KEY 读取 API Key。
    """
    client = anthropic.Anthropic()
    max_tokens = MAX_TOKENS_MAP.get(skill_name, 4096)

    print(f"[{skill_name}] 调用 Claude API (max_tokens={max_tokens})...", flush=True)

    full_response = []
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    ) as stream:
        for text_chunk in stream.text_stream:
            print(text_chunk, end="", flush=True)
            full_response.append(text_chunk)

    print()
    return "".join(full_response)
