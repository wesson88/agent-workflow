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
import re
import subprocess
import sys
import time
from pathlib import Path

from ..config import PROJECT_ROOT, VAULT_ROOT
from ..workflow import role_to_skill_dir, WorkflowStep
from .discussion import build_discussion_graph
from .brainstorm import build_brainstorm_graph
from .state import ProjectState


def _evaluate_skip_if(skip_if: dict, project: str) -> tuple[bool, str]:
    """评估工作流步骤的 skip_if 条件。

    支持的条件类型：
      frontmatter_eq:
        file: <vault 相对路径，可含 {project} 占位>
        key:  <frontmatter key>
        value: <期望值，字符串相等比较>

    返回 (should_skip, reason)。条件文件不存在或解析失败均返回 (False, ...)，
    保守不跳过——避免误杀。
    """
    cond = skip_if.get("frontmatter_eq")
    if not isinstance(cond, dict):
        return False, f"unknown skip_if shape: {skip_if!r}"

    rel = str(cond.get("file", "")).replace("{project}", project)
    key = str(cond.get("key", ""))
    expected = str(cond.get("value", ""))
    if not rel or not key:
        return False, "skip_if.frontmatter_eq 缺少 file/key"

    target = VAULT_ROOT / rel
    if not target.exists():
        return False, f"目标文件不存在：{rel}（条件不命中，按不跳过处理）"

    text = target.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        return False, f"{rel} 无 frontmatter"

    key_match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", fm_match.group(1), re.MULTILINE)
    if not key_match:
        return False, f"{rel} frontmatter 无 {key} 字段"

    actual = key_match.group(1).strip().strip('"').strip("'")
    if actual == expected:
        return True, f"{key}={actual} 匹配（来自 {rel}）"
    return False, f"{key}={actual} ≠ {expected}"

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
    from ..config import VAULT_ROOT

    system = _COMPRESS_SYSTEM.format(target_chars=target_chars)
    for rel_path in outputs:
        resolved = rel_path.replace("{project}", project)
        # 支持 glob 模式（如 "10-项目/{project}/指令/给后端-T*.md"）
        if "*" in resolved:
            matched = sorted(VAULT_ROOT.glob(resolved))
        else:
            matched = [VAULT_ROOT / resolved]

        if not matched:
            print(f"[post_compress] ⚠️ 无匹配文件，跳过压缩：{resolved}", file=sys.stderr)
            continue

        for full_path in matched:
            if not full_path.exists():
                print(f"[post_compress] ⚠️ 文件不存在，跳过压缩：{full_path.name}", file=sys.stderr)
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
            compressed_path = full_path.with_name(full_path.stem + "-压缩" + full_path.suffix)
            compressed_path.write_text(compressed, encoding="utf-8")
            print(f"[post_compress] ✅ 压缩完成：{compressed_path.name} ({len(compressed)}chars)")


def _run_pre_flight(
    project: str,
    task: str,
    instruction_file: str,
    split_limit_lines: int = 400,
    *,
    model_key: str = "claude-haiku-4-5",
    split_limit_tokens: int | None = None,
) -> list[dict] | None:
    """层四 Pre-flight：用 haiku 评估任务复杂度，返回 sub_tasks 列表或 None（不需拆分）。

    split_limit_tokens 优先于 split_limit_lines：
      - 传入 split_limit_tokens → 用 token_counter 精确估算是否需要拆分
      - 不传 → 沿用原有 split_limit_lines（行数估算，向后兼容）

    返回 None  → 单次调用即可
    返回 list  → 需要拆分，每个元素是 {focus, outputs}
    """
    from ..config import VAULT_ROOT
    from ..token_counter import count_tokens, estimate_tokens

    resolved = instruction_file.replace("{project}", project)
    full_path = VAULT_ROOT / resolved
    if not full_path.exists():
        print(f"[pre_flight] ⚠️ 指令文件不存在，跳过评估：{resolved}", file=sys.stderr)
        return None

    content = full_path.read_text(encoding="utf-8")

    # token 感知：计算指令文件自身的 token 数，用于日志和早期拆分判断
    if split_limit_tokens is not None:
        instruction_tokens = count_tokens(content, model_key)
        print(
            f"[pre_flight] 🔍 评估任务复杂度（{full_path.name},"
            f" {instruction_tokens} tokens / {len(content)} chars）..."
        )
    else:
        instruction_tokens = None
        print(f"[pre_flight] 🔍 评估任务复杂度（{full_path.name}, {len(content)} chars）...")

    try:
        raw = _call_haiku(_PREFLIGHT_SYSTEM, f"项目：{project}\n任务：{task}\n\n{content}")
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

    # 程序判断：token 感知优先，行数兜底
    if split_limit_tokens is not None:
        # 用 haiku 预估的行数换算 token（1 行 ≈ 15 tokens，保守估算）
        est_output_tokens = total * 15
        needs_split = est_output_tokens > split_limit_tokens or any(
            f.get("est_lines", 0) * 15 > split_limit_tokens * 0.6 for f in files
        )
        threshold_desc = f"{split_limit_tokens} tokens"
    else:
        needs_split = total > split_limit_lines or any(
            f.get("est_lines", 0) > split_limit_lines * 0.6 for f in files
        )
        threshold_desc = f"{split_limit_lines} 行"

    if not needs_split:
        print(f"[pre_flight] ✅ 预估产出在阈值 {threshold_desc} 以内，单次执行。")
        return None

    if not splits:
        print(f"[pre_flight] ⚠️ 需拆分但无 suggested_splits，降级为单次调用。", file=sys.stderr)
        return None

    print(f"[pre_flight] ✂️ 需要拆分为 {len(splits)} 个子任务：")
    for s in splits:
        print(f"  - focus={s.get('focus')}, outputs={s.get('outputs')}")
    return splits


# ── 执行策略（单一职责：仅负责运行 subprocess，不做判断/状态）─────────────────
# 永久错误码：不重试（2=参数错误/argparse，3=输出解析失败）
_PERMANENT_RC = {2, 3}


def _execute_single(
    main_py: Path,
    task: str,
    project: str,
    env: dict,
) -> int:
    """单次调用 main.py，返回 returncode。失败时指数退避重试最多 3 次。"""
    for attempt in range(3):
        try:
            rc = subprocess.run(
                [sys.executable, str(main_py), "--task", task, "--project", project],
                env=env,
                timeout=1800,
            ).returncode
        except subprocess.TimeoutExpired:
            print(f"[subprocess_timeout] {main_py.name} 超时（1800s），attempt={attempt + 1}/3", flush=True)
            rc = 1
        if rc == 0:
            return 0
        if rc in _PERMANENT_RC or attempt == 2:
            return rc
        wait = 2.0 * (2 ** attempt)
        print(f"[subprocess_retry] rc={rc}，等待 {wait:.0f}s 后重试（{attempt + 1}/3）", flush=True)
        time.sleep(wait)
    return rc  # unreachable but satisfies type checker


def _execute_with_subtasks(
    main_py: Path,
    task: str,
    project: str,
    sub_tasks: list[dict],
    env: dict,
) -> int:
    """按子任务列表逐一调用 main.py，任意子任务失败即返回非零 returncode。"""
    total = len(sub_tasks)
    for i, st in enumerate(sub_tasks, 1):
        focus = st.get("focus", f"子任务{i}")
        outputs = st.get("outputs", [])
        sub_task_desc = (
            f"{task} [子任务 {i}/{total}：{focus}，产出={outputs}]"
        )
        print(f"\n── 子任务 {i}/{total}: focus={focus} ──")
        rc = _execute_single(main_py, sub_task_desc, project, env)
        if rc != 0:
            return rc
    return 0


# ── 单角色 node（含 pre_flight + post_compress）─────────────────────────────────
def make_role_node(
    role_name: str,
    halt_on_failure: bool,
    *,
    post_compress: dict | None = None,
    pre_flight: dict | None = None,
    skip_if: dict | None = None,
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

        # 工作流层条件跳过（skip_if）：节点执行前评估，命中即跳过 subprocess
        if skip_if:
            should_skip, reason = _evaluate_skip_if(skip_if, state["project"])
            if should_skip:
                print(f"\n⏭️  跳过 {role_name}（skip_if 命中：{reason}）")
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
            print(f"\n✂️  拆分为 {len(sub_tasks_to_run)} 个子任务执行")
            rc = _execute_with_subtasks(
                main_py, state["task"], state["project"],
                sub_tasks_to_run, env,
            )
        else:
            rc = _execute_single(
                main_py, state["task"], state["project"], env,
            )

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


# ── 脑暴 orchestrator node（T2.6）─────────────────────────────────────
_BRAINSTORM_GRAPH = None  # 进程内单例


def _get_brainstorm_graph():
    global _BRAINSTORM_GRAPH
    if _BRAINSTORM_GRAPH is None:
        _BRAINSTORM_GRAPH = build_brainstorm_graph()
    return _BRAINSTORM_GRAPH


def make_brainstorm_loop_node(step: WorkflowStep, halt_on_failure: bool):
    """工厂函数：把 type=brainstorm-loop 的 WorkflowStep 包装成主图 node。

    主图 node 入口接收 ProjectState（项目级 state），内部洐生 BrainstormState
    去 invoke 脑暴 subgraph，跑完后回写主图 state（succeeded / failed / halted）。
    """
    name = step.name or "创意脑暴"
    if len(step.roles) != 3:
        raise ValueError(
            f"brainstorm-loop step 必须 3 角色，实际 {len(step.roles)}: {step.roles}"
        )
    diverger, challenger, scribe = step.roles
    max_rounds = step.max_rounds
    audit_rounds = step.audit_rounds or (3, 6)
    start_round = step.start_round
    readiness_threshold = step.readiness_threshold
    context_warn_tokens = step.context_warn_tokens

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            print(f"\n⏭️  跳过脑暴循环「{name}」（上游 halt）")
            return {"skipped": [f"脑暴:{name}"]}

        print(f"\n{'=' * 60}\n🧠 脑暴节点：{name}\n{'=' * 60}")

        sub_state = {
            "project": state["project"],
            "task": state.get("task", ""),
            "loop_name": name,
            "roles": (diverger, challenger, scribe),
            "max_rounds": max_rounds,
            "audit_rounds": audit_rounds,
            "readiness_threshold": readiness_threshold,
            "context_warn_tokens": context_warn_tokens,
            "start_round": start_round,
            "round_history": [],
            "halted": False,
            "finished": False,
        }
        try:
            final = _get_brainstorm_graph().invoke(sub_state)
        except Exception as e:
            print(f"\n❌ 脑暴「{name}」异常：{e}")
            patch = {"failed": [f"脑暴:{name}"]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        finish_reason = final.get("finish_reason")
        if finish_reason in ("halted", "role_failed", "readiness_missing", "readiness_parse_failed"):
            print(f"\n❌ 脑暴「{name}」中断（{finish_reason}）")
            patch = {"failed": [f"脑暴:{name}"]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        # ready_for_prd / ask_user / stop_low_value / max_rounds / pending_gate
        # 都视为正常退出（subgraph 已写 round_state.json，下游可读）
        print(f"\n✅ 脑暴「{name}」结束：finish_reason={finish_reason}")
        return {"succeeded": [f"脑暴:{name}"]}

    node.__name__ = f"brainstorm_{name.replace(' ', '_')}"
    return node
