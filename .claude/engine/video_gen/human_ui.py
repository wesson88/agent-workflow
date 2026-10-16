"""human_ui adapter：任务卡 + 人工闸。

用户照任务卡在工具网页（可灵/即梦/剪映…）里生成，按卡上的路径与命名放好文件，再 approve。
完成与否仍看文件：approve 了但文件不齐 → 换一个带"补件"后缀的 gate 再问一次（不会死循环，
也不会把口头 approve 当完成）。
"""

from __future__ import annotations

from pathlib import Path

from ..approval import ask_approval
from .base import GenResult, GenTask, missing_outputs, register_adapter

_KIND_LABEL = {
    "reference": "参考图（三视图/场景/道具）",
    "keyframe": "关键帧",
    "video": "视频 take",
    "tts": "配音",
    "compose": "成片合成",
}


def card_rel(task: GenTask) -> str:
    from ..artifact_registry import load_config
    root = load_config()[task.domain].replace("{project}", task.project)
    return f"{root}/执行/{task.kind}-{task.unit}-a{task.attempt}.md"


def _write_card(task: GenTask, root: Path, missing: list[str]) -> str:
    from ..config import VAULT_ROOT
    from ..obsidian_io import atomic_write_text
    rel = card_rel(task)
    lines = [
        "---", "type: generation-task", f"kind: {task.kind}", f"unit: {task.unit}",
        f"attempt: {task.attempt}", "---", "",
        f"# 执行任务：{_KIND_LABEL.get(task.kind, task.kind)} · {task.unit} · 第 {task.attempt} 次",
        "", task.brief or "", "",
        "## 照着做", "",
        *[f"- [[{r.rsplit('/', 1)[-1].removesuffix('.md')}]]（`{r}`）" for r in task.refs],
        "", f"## 产物放这里（资产根 `{root.as_posix()}`）", "",
        "每条至少一个文件；`*` 处按序号填 01、02…", "",
        *[f"- [{' ' if p in missing else 'x'}] `{p}`" for p in task.expected],
        "", "## 完成后", "",
        "回终端执行人工闸 approve（命令见运行输出），再重跑工作流。",
    ]
    if task.params:
        lines[-3:-3] = ["", "## 参数", "", *[f"- {k}: {v}" for k, v in task.params.items()]]
    atomic_write_text(VAULT_ROOT / rel, "\n".join(lines) + "\n")
    return rel


def run(task: GenTask, root: Path) -> GenResult:
    missing = missing_outputs(task, root)
    card = _write_card(task, root, missing)
    key, k = task.key, 0
    while True:
        decision = ask_approval(
            task.project, key,
            f"请按任务卡生成{_KIND_LABEL.get(task.kind, task.kind)}（{task.unit}），缺 {len(missing)} 项",
            node=f"generation:{task.kind}", refs=[card, *task.refs],
        )
        if decision != "approved":
            return GenResult("pending", tuple(missing), f"等待人工执行：{card}")
        if not missing_outputs(task, root):
            return GenResult("done")
        k += 1
        key = f"{task.key}:补件{k}"   # approve 了但文件不齐：换 key 再问


register_adapter("human_ui", run)
