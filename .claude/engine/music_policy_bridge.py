"""Read the vault's versioned music policy once for each project run."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from .config import VAULT_ROOT


def resolve_project_music_policy(project: str) -> dict:
    script = VAULT_ROOT / "00-系统/可执行技能/lyrics-audit/scripts/music_policy.py"
    if not script.is_file():
        return _legacy_vault_snapshot(project)
    spec = importlib.util.spec_from_file_location("vault_music_policy", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("music policy resolver could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    brief = VAULT_ROOT / "10-项目/music" / project / "inputs/创作简报.md"
    return module.resolve_policy(brief_path=brief if brief.is_file() else None)


def _legacy_vault_snapshot(project: str) -> dict:
    """Temporary compatibility for engine main while the vault agent PR awaits merge.

    These are the prior documented v6 values, with their old-vault source explicit.
    Remove this bridge after the vault resolver is available in every deployment.
    """
    brief = VAULT_ROOT / "10-项目/music" / project / "inputs/创作简报.md"
    duration = 240  # 用户 2026-11-01 确认的普通歌曲默认交付上限。
    model = "v6"
    if brief.is_file():
        text = brief.read_text(encoding="utf-8-sig")
        match = re.search(r"^\s*-\s*时长上限秒\s*[：:]\s*(\d+)\b", text, re.M)
        if match:
            duration = int(match.group(1))
        model_match = re.search(r"^\s*-\s*模型档位\s*[：:]\s*(v6(?:-wild|-mini)?)\b", text, re.I | re.M)
        if model_match:
            model = model_match.group(1).lower()
    if not 0 < duration <= 480:
        raise ValueError("旧版 vault 项目交付上限须大于零且不超过既有 v6 单次能力档案 480 秒")
    def item(value, basis):
        return {"value": value, "basis": basis, "source": "legacy_vault_fallback"}
    source = "旧版 vault 的 Suno-执行.schema 与 Suno-UI-字符上限记录；等待 vault agent PR 合并"
    values = {
        "max_duration_s": item(duration, "用户确认默认值或项目简报 §9 覆盖；" + source),
        "style_hard_chars": item(1000, "既有 v6 Style 输入框核验值；" + source),
        "style_target_chars": item(950, "既有 1000 上限减 50 人工余量；" + source),
        "lyrics_hard_chars": item(5000, "既有 v6 Lyrics 输入框核验值；" + source),
        "title_hard_chars": item(100, "既有 v6 Title 输入框核验值；" + source),
        "tool_max_duration_s": item(480, "既有 v6 官方单次能力记录；" + source),
    }
    return {"schema_version": "legacy-vault-compat", "model": model,
            "brief": str(brief) if brief.is_file() else None, "values": values,
            "acoustic_guidance": {"status": "unspecified"},
            "compatibility_note": "vault 参数解析器未安装；使用旧版已记录值，待 vault agent PR 合并"}


def format_music_policy(snapshot: dict) -> str:
    values = snapshot["values"]
    keys = ("max_duration_s", "style_hard_chars", "style_target_chars",
            "lyrics_hard_chars", "title_hard_chars", "tool_max_duration_s")
    rows = [f"- {key}: {values[key]['value']}（{values[key]['source']}；{values[key]['basis']}）"
            for key in keys]
    if snapshot.get("compatibility_note"):
        rows.append(f"- 兼容状态：{snapshot['compatibility_note']}")
    guidance = snapshot["acoustic_guidance"]
    if guidance["status"] == "advisory":
        rows.append(f"- 发布平台声学建议：{guidance['platform']}，目标 {guidance['integrated_lufs_target']} LUFS，真峰值低于 {guidance['true_peak_dbtp_below']} dBTP；仅建议，依据：{guidance['basis']}")
    elif guidance["status"] == "unconfigured":
        rows.append(f"- 发布平台 {guidance['platform']} 尚无声学档案；请人工核定目标，不凭空推断数字")
    return "本次生效音乐参数（来自同一份版本化快照；§3 Lyrics 只放可复制内容）：\n" + "\n".join(rows)
