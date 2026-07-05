"""
capability_executor/audit_writer.py — 写 vault 调用日志 + 全局 audit.jsonl。

规范：`00-系统/规则/capability注册表规范.md §6`

**双写策略**：
- vault `20-知识/能力注册表/<root>/调用日志/{ts}-{project}.md`：Obsidian dataview 聚合用
- `.claude/audit.jsonl`：全局引擎事件流（跨 capability 分析用，复用现有 append_audit）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import VAULT_ROOT
from .base import ExecutorResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _ts_stamp() -> str:
    """audit 文件名用的时间戳，格式 `YYYYMMDD-HHMM`。依据：规范 §2.2 明标。"""
    return datetime.now().strftime("%Y%m%d-%H%M")


def _input_hash(inputs: dict[str, Any]) -> str:
    """sha256(inputs 序列化)。依据：规范 §6.1 明标。用 sort_keys 保证同 inputs 得同 hash。"""
    payload = json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render_log_path(
    log_to_pattern: str, ts_stamp: str, project: str
) -> Path:
    """把 audit.log_to 里的 `{ts}` / `{project}` 替换成实际值，返回绝对路径。"""
    rel = log_to_pattern.replace("{ts}", ts_stamp).replace("{project}", project)
    p = Path(rel)
    if not p.is_absolute():
        p = VAULT_ROOT / p
    return p


def write_audit(
    manifest: dict,
    project: str,
    inputs: dict[str, Any],
    result: ExecutorResult,
    *,
    token_consumed: int | None = None,
) -> Path:
    """按 manifest.audit.log_to 渲染路径，写审计 markdown。

    fields 按规范 §6.1 必填：ts / project / capability_id / version /
    input_hash / exit_code / duration_s / artifact_path / token_consumed / error

    双写：
    - vault markdown（frontmatter + body）
    - `.claude/audit.jsonl` 一条 `type=capability_invoke` 记录（复用 common.append_audit）
    """
    ts_iso = _utc_now_iso()
    ts_stamp = _ts_stamp()
    dest = _render_log_path(manifest["audit"]["log_to"], ts_stamp, project)
    dest.parent.mkdir(parents=True, exist_ok=True)

    ihash = _input_hash(inputs)
    cap_id = manifest["id"]
    cap_ver = manifest["version"]
    exit_code = result.exit_code
    duration_s = round(result.duration_s, 3)
    artifact_str = ", ".join(str(p) for p in result.artifact_paths) or "-"
    error_str = result.error or "-"

    # ── vault 侧 markdown ────────────────────────────────
    md_content = _render_markdown(
        ts_iso=ts_iso,
        cap_id=cap_id,
        cap_ver=cap_ver,
        project=project,
        inputs=inputs,
        ihash=ihash,
        result=result,
        artifact_str=artifact_str,
        error_str=error_str,
        token_consumed=token_consumed,
    )
    _atomic_write(dest, md_content)

    # ── 全局 audit.jsonl 事件（正向依赖 engine.audit，P10.5 A1 修）─────
    from ..audit import append_audit
    append_audit({
        "timestamp": ts_iso,
        "type": "capability_invoke",
        "capability_id": cap_id,
        "version": cap_ver,
        "project": project,
        "input_hash": ihash,
        "exit_code": exit_code,
        "duration_s": duration_s,
        "artifact_paths": [str(p) for p in result.artifact_paths],
        "token_consumed": token_consumed,
        "error": result.error,
        "audit_log_path": str(dest),
    })
    # append_audit 内部已 fail-safe（不抛异常）

    return dest


def _render_markdown(
    *,
    ts_iso: str,
    cap_id: str,
    cap_ver: str,
    project: str,
    inputs: dict[str, Any],
    ihash: str,
    result: ExecutorResult,
    artifact_str: str,
    error_str: str,
    token_consumed: int | None,
) -> str:
    """按规范 §6.2 示例渲染 markdown。"""
    lines: list[str] = []
    lines.append("---")
    lines.append("type: capability-audit")
    lines.append(f"capability: {cap_id}")
    lines.append(f"version: {cap_ver}")
    lines.append(f"project: {project}")
    lines.append(f"ts: {ts_iso}")
    lines.append(f"exit_code: {result.exit_code}")
    lines.append(f"duration_s: {round(result.duration_s, 3)}")
    lines.append(f"input_hash: {ihash}")
    if token_consumed is not None:
        lines.append(f"token_consumed: {token_consumed}")
    lines.append("---")
    lines.append("")
    lines.append(f"# 调用：{cap_id} @ {ts_iso}")
    lines.append("")
    lines.append("## 输入")
    for k, v in sorted(inputs.items()):
        vs = str(v)
        if len(vs) > 200:
            vs = vs[:200] + "...(截断)"
        lines.append(f"- **{k}**: `{vs}`")
    lines.append(f"- **input_hash**: `{ihash}`")
    lines.append("")
    lines.append("## 输出")
    lines.append(f"- artifact_path: `{artifact_str}`")
    lines.append("")
    lines.append("## 性能")
    lines.append(f"- exit_code: {result.exit_code}")
    lines.append(f"- duration_s: {round(result.duration_s, 3)}")
    if token_consumed is not None:
        lines.append(f"- token_consumed: {token_consumed}")
    if result.error:
        lines.append("")
        lines.append("## 错误")
        err = error_str
        if len(err) > 500:
            err = err[:500] + "...(截断至 500 chars)"
        lines.append(f"```\n{err}\n```")
    lines.append("")
    if result.stdout.strip():
        lines.append("## stdout（前 1000 chars）")
        so = result.stdout[:1000]
        lines.append(f"```\n{so}\n```")
        lines.append("")
    if result.stderr.strip():
        lines.append("## stderr（前 1000 chars）")
        se = result.stderr[:1000]
        lines.append(f"```\n{se}\n```")
        lines.append("")
    return "\n".join(lines) + "\n"


def _atomic_write(dest: Path, content: str) -> None:
    """tmp + os.replace 原子落盘。复用 common.write_output_atomic 语义（无循环 import）。"""
    from tempfile import NamedTemporaryFile
    dest.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=dest.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
        newline="\n",
    ) as tf:
        tf.write(content)
        tmp = tf.name
    Path(tmp).replace(dest)
