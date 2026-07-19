"""
engine/role_invoke.py — 角色调用统一接口（F7 阶段 B · 2026-07-18）

设计：[[F7-invoke_role-联合设计-2026-07-18]] §2。
背景：此前 workflow 节点调角色 = 各自拼 subprocess argv + env 字符串约定
（AGENT_SELECTED_MODULE_ID / AGENT_CONTRACT_OVERRIDES 塞 JSON），无类型、
无结构化返回。本模块给出正式接口；v1 唯一实现 = subprocess（行为等价包装），
mode="in_process" 预留给 role_runner（架构演进第 3 步）落地后。

调用点（graph 层三处）全部经 invoke_role 进：
- nodes.make_role_node（linear 步骤，含 pre_flight 子任务扇出）
- brainstorm._execute_brainstorm_role（--round 透传）
- module_dev_loop_node._dispatch_engineer（module_id + engineer overrides）

subprocess 重试语义（自 nodes._execute_single 迁入，单一来源）：
- 3 次指数退避（2s/4s/8s）；rc ∈ _PERMANENT_RC={2,3} 不重试
  （2=参数错误/argparse，3=输出解析失败）
- 超时 1800s：F7 阶段 A 后这只是外层最后防线——真正的 hang 防护
  （心跳 300s / 僵尸检测 / CLI 层 1800s）在内层 llm._call_cli。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import PROJECT_ROOT


# 永久错误码：不重试（2=参数错误/argparse，3=输出解析失败）
_PERMANENT_RC = {2, 3}
_SUBPROCESS_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class RoleInvocation:
    """一次角色调用的全部输入（原 subprocess argv + env 约定的类型化形态）。"""
    role: str                                # 中文名或别名（如 dev_backend）
    task: str
    project: str
    module_id: str | None = None             # 单模块聚焦（原 env AGENT_SELECTED_MODULE_ID）
    contract_overrides: dict | None = None   # 原 env AGENT_CONTRACT_OVERRIDES(JSON)
    round: int | None = None                 # brainstorm 轮次（原 CLI --round）
    extra_env: dict[str, str] = field(default_factory=dict)  # 逃生口


@dataclass(frozen=True)
class RoleResult:
    """一次角色调用的结构化结果。

    subprocess 模式下 outputs/tokens 为 None（产物在 vault、token 在
    audit.jsonl）；in_process 模式起可直接携带。
    """
    status: Literal["success", "failed", "permanent_failed"]
    returncode: int
    role: str
    elapsed_s: float
    outputs: tuple[str, ...] | None = None
    tokens: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _execute_single(
    main_py: Path,
    task: str,
    project: str,
    env: dict,
    *,
    extra_args: list[str] | None = None,
    log_prefix: str = "subprocess",
) -> int:
    """单次调用 main.py，返回 returncode。失败时指数退避重试最多 3 次。

    2026-07-18 F7 阶段 B 自 graph/nodes.py 迁入（接口模块是它的稳定居所；
    graph 层与 brainstorm 曾各持一份逐行复制）。
    """
    argv = [
        sys.executable, str(main_py),
        "--task", task, "--project", project,
        *(extra_args or []),
    ]
    for attempt in range(3):
        try:
            rc = subprocess.run(
                argv,
                env=env,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            ).returncode
        except subprocess.TimeoutExpired:
            print(
                f"[{log_prefix}_timeout] {main_py.name} 超时"
                f"（{_SUBPROCESS_TIMEOUT_SECONDS}s），attempt={attempt + 1}/3",
                flush=True,
            )
            rc = 1
        if rc == 0:
            return 0
        if rc in _PERMANENT_RC or attempt == 2:
            return rc
        wait = 2.0 * (2 ** attempt)
        print(f"[{log_prefix}_retry] rc={rc}，等待 {wait:.0f}s 后重试（{attempt + 1}/3）", flush=True)
        time.sleep(wait)
    return rc  # unreachable but satisfies type checker


def _build_env(inv: RoleInvocation) -> dict:
    """环境变量组装（原三处调用点各自手拼的统一实现）。

    set-or-pop 语义：module_id / contract_overrides 为 None 时主动 pop，
    避免继承 os.environ 里的陈旧值（原 nodes.make_role_node 行为）。
    """
    env = os.environ.copy()
    env["PROJECT"] = inv.project
    env["TASK"] = inv.task
    if inv.module_id:
        env["AGENT_SELECTED_MODULE_ID"] = inv.module_id
    else:
        env.pop("AGENT_SELECTED_MODULE_ID", None)
    if inv.contract_overrides:
        env["AGENT_CONTRACT_OVERRIDES"] = json.dumps(
            inv.contract_overrides, ensure_ascii=False
        )
    else:
        env.pop("AGENT_CONTRACT_OVERRIDES", None)
    if inv.extra_env:
        env.update(inv.extra_env)
    return env


def invoke_role(
    inv: RoleInvocation,
    *,
    mode: str = "subprocess",
    log_prefix: str = "subprocess",
) -> RoleResult:
    """统一角色调用入口。v1 唯一实现 = subprocess（行为等价包装现有语义）。"""
    if mode != "subprocess":
        raise NotImplementedError(
            f"mode='{mode}' 未实现（in_process 留给 role_runner，"
            f"见架构演进第 3 步）"
        )

    from .workflow import role_to_skill_dir  # 延迟：避免 import 链提前触发 vault 扫描

    t0 = time.monotonic()
    try:
        skill_dir = role_to_skill_dir(inv.role)
    except Exception as e:
        return RoleResult(
            status="permanent_failed", returncode=-2, role=inv.role,
            elapsed_s=time.monotonic() - t0,
            error=f"角色解析失败：{e}",
        )
    main_py = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"
    if not main_py.is_file():
        return RoleResult(
            status="permanent_failed", returncode=-2, role=inv.role,
            elapsed_s=time.monotonic() - t0,
            error=f"skill 缺 main.py：{main_py}",
        )

    # 产物注册表 v0.3：消费端前置检查（warn 打日志继续；fail 不起 subprocess）
    from .artifact_check import run_check
    mode, consume_issues = run_check("consume", inv.role, inv.project)
    if consume_issues and mode == "fail":
        return RoleResult(
            status="permanent_failed", returncode=-3, role=inv.role,
            elapsed_s=time.monotonic() - t0,
            error="消费端产物缺失（AGENT_ARTIFACT_CHECK=fail）："
                  + "；".join(consume_issues),
        )

    env = _build_env(inv)
    extra_args = ["--round", str(inv.round)] if inv.round is not None else None
    rc = _execute_single(
        main_py, inv.task, inv.project, env,
        extra_args=extra_args, log_prefix=log_prefix,
    )
    elapsed = time.monotonic() - t0
    if rc == 0:
        status = "success"
    elif rc in _PERMANENT_RC:
        status = "permanent_failed"
    else:
        status = "failed"
    error = None if rc == 0 else f"exit_code={rc}"

    # 产物注册表 v0.3：产出端检查（warn 打日志；fail 降 success → failed）
    if rc == 0:
        mode, produce_issues = run_check("produce", inv.role, inv.project)
        if produce_issues and mode == "fail":
            status = "failed"
            error = ("产出端产物缺失（AGENT_ARTIFACT_CHECK=fail）："
                     + "；".join(produce_issues))

    return RoleResult(
        status=status, returncode=rc, role=inv.role, elapsed_s=elapsed,
        error=error,
    )
