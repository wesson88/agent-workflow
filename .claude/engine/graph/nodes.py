"""
graph/nodes.py — LangGraph node 工厂

Phase 4a：单角色 node = subprocess 包装 `.claude/skills/<role>/main.py`
Phase 4b：新增 make_discussion_node = 多角色讨论 subgraph 包装
Phase 5 新增：
  - post_compress：角色执行完成后，用 haiku 压缩指定输出文件（层三）
  - pre_flight：执行前用 haiku 评估任务复杂度，决定是否拆分为 sub_tasks（层四）
每个 node 接收 ProjectState，返回 patch dict（LangGraph 自动 merge）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..config import PROJECT_ROOT
from ..workflow import role_to_skill_dir, WorkflowStep
from .discussion import build_discussion_graph
from .state import ProjectState

# ── 层三：post_compress 压缩提示词 ─────────────────────────────────────────────
_COMPRESS_SYSTEM = """你是一个文档压缩专家。
你的任务：将输入的技术指令文档压缩为精炼的可执行任务清单。

规则：
- 保留：接口定义、验收标准、路径约束、必须实现的功能点
- 删除：背景说明、架构推理、设计原因、重复的上下文描述
- 格式：保持 Markdown，任务用编号列表，每条 ≤ 2 行
- 体积目标：压缩后 ≤ {target_chars} 字符
- 禁止新增任何原文没有的需求
- 输出直接是压缩后的文档，不加任何解释前缀
"""

_PREFLIGHT_SYSTEM = """你是任务复杂度评估专家。
分析输入的技术指令文档，列出本次需要产出的所有文件及预估行数。

严格按以下 JSON 格式输出，不加任何其他文字：
{
  "files": [
    {"path": "src/backend/routes.py", "est_lines": 120, "layer": "路由层"},
    {"path": "src/backend/db.py", "est_lines": 80, "layer": "数据库层"}
  ],
  "total_lines": 200,
  "suggested_splits": [
    {"focus": "路由层", "outputs": ["src/backend/routes.py"]},
    {"focus": "数据库层", "outputs": ["src/backend/db.py"]}
  ]
}

"suggested_splits" 按功能层/文件独立性分组，每组预估行数 ≤ 400 行。
"""


def _call_haiku(system: str, user: str) -> str:
    """调用 haiku 模型（轻量、低成本）。走 engine.llm 统一路由。"""
    from ..llm import call_llm

    return call_llm(system, user, model="claude-haiku-4-5", max_tokens=4096, print_stream=False)


def _run_post_compress(
    project: str,
    outputs: list[str],
    target_chars: int = 8000,
) -> None:
    """层三：对指定输出文件调用 haiku 压缩，生成 <name>-压缩.md 副本。
    原文件保留供人工审阅，压缩版供下游角色读取。
    """
    vault_root = PROJECT_ROOT.parent  # 假设 vault 在 agent-workflow 同级，实际由 config 决定
    from ..config import VAULT_ROOT

    vault_root = VAULT_ROOT

    system = _COMPRESS_SYSTEM.format(target_chars=target_chars)
    for rel_path in outputs:
        resolved = rel_path.replace("{project}", project)
        full_path = vault_root / resolved
        if not full_path.exists():
            print(f"[post_compress] ⚠️ 文件不存在，跳过压缩：{resolved}", file=sys.stderr)
            continue
        original = full_path.read_text(encoding="utf-8")
        original_len = len(original)
        if original_len <= target_chars:
            print(f"[post_compress] ✅ {full_path.name} ({original_len}chars) ≤ 目标，跳过压缩。")
            continue
        print(f"[post_compress] 🗜️ 压缩 {full_path.name} ({original_len}chars → 目标≤{target_chars}chars)...")
        try:
            compressed = _call_haiku(system, original)
        except Exception as e:
            print(f"[post_compress] ❌ haiku 调用失败：{e}", file=sys.stderr)
            continue
        # 写入 <stem>-压缩.md（如 给后端-压缩.md）
        compressed_path = full_path.with_name(full_path.stem + "-压缩" + full_path.suffix)
        compressed_path.write_text(compressed, encoding="utf-8")
        print(f"[post_compress] ✅ 压缩完成：{compressed_path.name} ({len(compressed)}chars)")


def _run_pre_flight(
    project: str,
    task: str,
    instruction_file: str,
    split_limit_lines: int = 400,
) -> list[dict] | None:
    """层四 Pre-flight：用 haiku 评估任务复杂度，返回 sub_tasks 列表或 None（不需拆分）。
    返回 None  → 单次调用即可
    返回 list  → 需要拆分，每个元素是 {focus, outputs}
    """
    from ..config import VAULT_ROOT

    resolved = instruction_file.replace("{project}", project)
    full_path = VAULT_ROOT / resolved
    if not full_path.exists():
        print(f"[pre_flight] ⚠️ 指令文件不存在，跳过评估：{resolved}", file=sys.stderr)
        return None

    content = full_path.read_text(encoding="utf-8")
    print(f"[pre_flight] 🔍 评估任务复杂度（{full_path.name}, {len(content)}chars）...")
    try:
        raw = _call_haiku(_PREFLIGHT_SYSTEM, f"项目：{project}\n任务：{task}\n\n{content}")
        # 提取 JSON（haiku 有时会在前后加说明文字）
        start = raw.find("{")
        end = raw.rfind("}") + 1
        result = json.loads(raw[start:end])
    except Exception as e:
        print(f"[pre_flight] ⚠️ 评估失败（{e}），降级为单次调用。", file=sys.stderr)
        return None

    total = result.get("total_lines", 0)
    files = result.get("files", [])
    splits = result.get("suggested_splits", [])

    print(f"[pre_flight] 📊 预估产出：{len(files)} 个文件，合计 ~{total} 行")
    for f in files:
        print(f"  - {f['path']} (~{f['est_lines']} 行, {f.get('layer','')})")

    # 程序判断：不依赖模型决策
    needs_split = total > split_limit_lines or any(
        f.get("est_lines", 0) > split_limit_lines * 0.6 for f in files
    )
    if not needs_split:
        print(f"[pre_flight] ✅ 总行数 {total} ≤ {split_limit_lines}，单次执行。")
        return None

    if not splits:
        print(f"[pre_flight] ⚠️ 需拆分但无 suggested_splits，降级为单次调用。", file=sys.stderr)
        return None

    print(f"[pre_flight] ✂️ 需要拆分为 {len(splits)} 个子任务：")
    for s in splits:
        print(f"  - focus={s.get('focus')}, outputs={s.get('outputs')}")
    return splits


# ── 单角色 node（含 pre_flight + post_compress）─────────────────────────────────
def make_role_node(
    role_name: str,
    halt_on_failure: bool,
    *,
    post_compress: dict | None = None,
    pre_flight: dict | None = None,
):
    """工厂函数：返回一个 LangGraph node 函数。
    role_name 是 vault 角色 frontmatter 的 role 字段（中文名）。
    halt_on_failure 由工作流模板决定，闭包单获。

    post_compress（层三）: {
        "model": "claude-haiku-4-5",   # 固定，仅 haiku
        "target_chars": 8000,
        "outputs": ["10-项目/{project}/指令/给后端.md"]
    }
    pre_flight（层四）: {
        "instruction_file": "10-项目/{project}/指令/给后端.md",
        "split_limit_lines": 400
    }
    """
    skill_dir = role_to_skill_dir(role_name)
    main_py = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"

    def node(state: ProjectState) -> dict:
        # 上游 halt 时跳过本 node
        if state.get("halted"):
            print(f"\n⏭️  跳过 {role_name}（上游 halt）")
            return {"skipped": [role_name]}

        print(f"\n{'=' * 60}\n▶ 运行 {role_name} ({skill_dir})  项目={state['project']}\n{'=' * 60}")

        # ── 层四 Pre-flight ──────────────────────────────────────────
        sub_tasks_to_run: list[dict] | None = None
        if pre_flight:
            sub_tasks_to_run = _run_pre_flight(
                project=state["project"],
                task=state["task"],
                instruction_file=pre_flight["instruction_file"],
                split_limit_lines=pre_flight.get("split_limit_lines", 400),
            )

        env = os.environ.copy()
        env["PROJECT"] = state["project"]
        env["TASK"] = state["task"]

        if sub_tasks_to_run:
            # 拆分执行：每个 sub_task 独立一次 main.py 调用
            print(f"\n✂️  拆分为 {len(sub_tasks_to_run)} 个子任务执行")
            for i, st in enumerate(sub_tasks_to_run, 1):
                focus = st.get("focus", f"子任务{i}")
                outputs = st.get("outputs", [])
                sub_task_desc = f"{state['task']} [子任务 {i}/{len(sub_tasks_to_run)}：{focus}，产出={outputs}]"
                print(f"\n── 子任务 {i}/{len(sub_tasks_to_run)}: focus={focus} ──")
                rc = subprocess.run(
                    [sys.executable, str(main_py),
                     "--task", sub_task_desc,
                     "--project", state["project"]],
                    env=env,
                ).returncode
                if rc != 0:
                    print(f"\n❌ {role_name} 子任务 {i} 失败（exit={rc}）")
                    patch = {"failed": [role_name]}
                    if halt_on_failure:
                        patch["halted"] = True
                    return patch
            print(f"\n✅ {role_name} 所有子任务完成")
        else:
            # 单次执行（原有逻辑）
            rc = subprocess.run(
                [sys.executable, str(main_py),
                 "--task", state["task"],
                 "--project", state["project"]],
                env=env,
            ).returncode
            if rc != 0:
                print(f"\n❌ {role_name} 失败（exit={rc}）")
                patch = {"failed": [role_name]}
                if halt_on_failure:
                    patch["halted"] = True
                    print("中断后续步骤（halt_on_failure=True）")
                return patch

        print(f"\n✅ {role_name} 完成")

        # ── 层三 post_compress ───────────────────────────────────────
        if post_compress:
            _run_post_compress(
                project=state["project"],
                outputs=post_compress.get("outputs", []),
                target_chars=post_compress.get("target_chars", 8000),
            )

        return {"succeeded": [role_name]}

    node.__name__ = f"node_{skill_dir}"
    return node


# ── 讨论 node（Phase 4b）────────────────────────────────────────────────────────
_DISCUSSION_GRAPH = None  # 进程内单例（subgraph 不依赖 step config，可复用）


def _get_discussion_graph():
    global _DISCUSSION_GRAPH
    if _DISCUSSION_GRAPH is None:
        _DISCUSSION_GRAPH = build_discussion_graph()
    return _DISCUSSION_GRAPH


def make_discussion_node(step: WorkflowStep, halt_on_failure: bool):
    """工厂函数：把 type=discussion 的 WorkflowStep 包装成主图 node。
    主图 node 入口接收 ProjectState（项目级 state），内部洐生 DiscussionState
    去 invoke 讨论 subgraph，跑完后回写主图 state。
    """
    name = step.name or "未命名讨论"
    participants = step.roles
    moderator = step.moderator
    max_rounds = step.max_rounds
    topic_template = step.topic_template or "评审本项目至此为止的所有产出"

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            print(f"\n⏭️  跳过讨论「{name}」（上游 halt）")
            return {"skipped": [f"讨论:{name}"]}

        print(f"\n{'=' * 60}\n💬 讨论节点：{name}\n  参与者：{list(participants)}\n{'=' * 60}")

        topic = topic_template.replace("{project}", state["project"]).replace("{task}", state["task"])
        sub_state = {
            "project": state["project"],
            "task": state["task"],
            "topic": topic,
            "participants": participants,
            "moderator": moderator,
            "max_rounds": max_rounds,
            "discussion_name": name,
            "messages": [],
            "current_round": 0,
            "next_speaker": None,
            "finished": False,
        }
        try:
            _get_discussion_graph().invoke(sub_state)
        except Exception as e:
            print(f"\n❌ 讨论「{name}」异常：{e}")
            patch = {"failed": [f"讨论:{name}"]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        print(f"\n✅ 讨论「{name}」完成")
        return {"succeeded": [f"讨论:{name}"]}

    node.__name__ = f"discussion_{name.replace(' ', '_')}"
    return node
