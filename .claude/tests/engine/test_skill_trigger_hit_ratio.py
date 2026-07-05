"""
test_skill_trigger_hit_ratio.py — P6 触发器命中判据

两层判据（方案 δ · 2026-07-04 决策）：

**层 1 — 精准命中**（False Negative 防线，硬 assert）
  每个 skill 在自己核心域场景下必被召回。9 条精准命中断言覆盖 SE 域
  4 个后端 + 5 个架构师 + 1 个前端 skill。

**层 2 — Baseline snapshot regression**（回归防线，硬 assert + 可更新）
  每次跑记录 3 角色 × 3 场景 = 9 采样的命中 skill 集合到
  `.claude/tests/baselines/skill_trigger.json`。后续 diff 命中集合任何变化
  → fail 强制人工审阅。审阅确认后 `UPDATE_SKILL_BASELINE=1 pytest ...` 更新
  baseline。

**明确不做**（历史设计 & 判定后移除）：
- 60% 命中率门槛：拍脑袋无依据。见 [[阈值-无依据-60%触发器采样案例-2026-07-04]]
- "每场景至少 1 hit"弱下限：always=True skill 存在时形同虚设
- 40% 综合平均兜底：数字拍脑袋

**长期演进**：见 98-待办 α 方案（skill frontmatter 加 test_scenarios 字段，
false positive 判据从 skill 作者主动声明的 ground truth 反演）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engine.config import PROJECT_ROOT, VAULT_ROOT
from engine.skill_trigger import discover_role_skills


SKILL_ROOT = VAULT_ROOT / "20-知识" / "角色技能" / "se"
BASELINE_PATH = PROJECT_ROOT / ".claude" / "tests" / "baselines" / "skill_trigger.json"
UPDATE_ENV_VAR = "UPDATE_SKILL_BASELINE"


# ── 场景 task_text（真实业务片段） ────────────────────────────
# 依据：手造场景，覆盖每个 SE 域 skill 的关键词命中场景。
# ⚠️ 手造非实战溯源——P7+ 校准计划见 98-待办 α 方案（skill 补 test_scenarios）
SCENARIO_BACKEND_ENV = """\
实现后端服务：读环境变量 DB_PATH 初始化 sqlite3 连接，通过 FastAPI lifespan
钩子暴露 app.state。禁止在模块顶层 os.environ.get() 求值。fetchone 结果为空时
必须先守卫再取 [0]。
"""

SCENARIO_BACKEND_STATIC = """\
新增静态资源路由 StaticFiles，挂载 /static/。用 resolve_path 解析相对路径，
避免 FileResponse 泄漏项目根路径。
"""

SCENARIO_BACKEND_ASYNC = """\
用 FastAPI 实现 API 端点：app.state 里放共享 sqlite3 连接，注意 check_same_thread
与 async 路由的兼容性。fetchone 返回空集时守卫处理。
"""

SCENARIO_ARCH_BUDGET = """\
架构设计：估算后端代码量预算，基线约 60 行，加上失败模式增量。
识别失败模式并列穷举表。
"""

SCENARIO_ARCH_DEPS = """\
依赖锁定策略：pip freeze 生成 requirements.txt；前端用 npm ci 保证可复现。
降级路径独占覆盖：主路径失败时走 fallback 分支。
"""

SCENARIO_ARCH_REVIEW = """\
架构评审后产出评审决策文档，与系统设计保持 lockstep 文档同步。评审记录
包含所有的失败模式和异常路径覆盖。
"""

SCENARIO_FRONTEND_FETCH = """\
调用后端 API：fetch('/api/entries')，检查 res.ok 后再取 res.json()。错误
分支走 res.text() 打印诊断信息。
"""

SCENARIO_FRONTEND_STATE = """\
前端表单状态管理：用 useState 存 formData，onSubmit 时 fetch POST 提交,
res.ok 时刷新列表。
"""

SCENARIO_FRONTEND_ROUTING = """\
新增路由页 /entries：GET /api/entries 拉数据，fetch 响应检查后渲染列表。
"""


# 场景集：baseline snapshot 与 informational 打印共用的采样点定义
ROLE_SCENARIOS: list[tuple[str, list[tuple[str, str]]]] = [
    ("后端工程师", [
        ("env", SCENARIO_BACKEND_ENV),
        ("static", SCENARIO_BACKEND_STATIC),
        ("async", SCENARIO_BACKEND_ASYNC),
    ]),
    ("架构师", [
        ("budget", SCENARIO_ARCH_BUDGET),
        ("deps", SCENARIO_ARCH_DEPS),
        ("review", SCENARIO_ARCH_REVIEW),
    ]),
    ("前端工程师", [
        ("fetch", SCENARIO_FRONTEND_FETCH),
        ("state", SCENARIO_FRONTEND_STATE),
        ("routing", SCENARIO_FRONTEND_ROUTING),
    ]),
]


# ── 工具 ────────────────────────────────────────────────
def _hits(role_dir: Path, task_text: str) -> set[str]:
    """返回命中 skill 的 stem 集合。"""
    return {p.stem for p, _ in discover_role_skills(role_dir, task_text)}


def _current_matrix() -> dict[str, dict[str, list[str]]]:
    """当前 3 角色 × 3 场景 = 9 采样点的命中 stem（排序稳定，便于 JSON diff）。"""
    result: dict[str, dict[str, list[str]]] = {}
    for role, scenarios in ROLE_SCENARIOS:
        role_dir = SKILL_ROOT / role
        result[role] = {}
        for name, task in scenarios:
            result[role][name] = sorted(_hits(role_dir, task))
    return result


def _load_baseline() -> dict | None:
    if not BASELINE_PATH.is_file():
        return None
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _save_baseline(matrix: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(matrix, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ── 层 1：精准命中（False Negative 防线，硬 assert）─────────────
class TestPrecisionHits:
    """每 skill 在自己核心域场景必被召回；触发器 keyword 漂移即会 fail。"""

    def test_backend_B1_hits_env_scenario(self):
        stems = _hits(SKILL_ROOT / "后端工程师", SCENARIO_BACKEND_ENV)
        assert any(s.startswith("B1-") for s in stems), (
            f"B1（环境变量）在 env 场景应命中；实测命中：{stems}"
        )

    def test_backend_B6_hits_static_scenario(self):
        stems = _hits(SKILL_ROOT / "后端工程师", SCENARIO_BACKEND_STATIC)
        assert any(s.startswith("B6-") for s in stems), (
            f"B6（静态资源）在 static 场景应命中；实测命中：{stems}"
        )

    def test_backend_B7_hits_async_scenario(self):
        stems = _hits(SKILL_ROOT / "后端工程师", SCENARIO_BACKEND_ASYNC)
        assert any(s.startswith("B7-") for s in stems), (
            f"B7（FastAPI async）在 async 场景应命中；实测命中：{stems}"
        )

    def test_backend_B5_always_hits(self):
        """B5 空集守卫 always=True，任何场景都应命中。"""
        for scenario in (SCENARIO_BACKEND_ENV, SCENARIO_BACKEND_STATIC, SCENARIO_BACKEND_ASYNC):
            stems = _hits(SKILL_ROOT / "后端工程师", scenario)
            assert any(s.startswith("B5-") for s in stems), (
                f"B5（always=True）应恒命中；场景命中：{stems}"
            )

    def test_arch_A2_hits_budget_scenario(self):
        stems = _hits(SKILL_ROOT / "架构师", SCENARIO_ARCH_BUDGET)
        assert any(s.startswith("A2-") for s in stems), (
            f"A2（失败模式）在 budget 场景应命中；实测：{stems}"
        )

    def test_arch_A3_hits_deps_scenario(self):
        stems = _hits(SKILL_ROOT / "架构师", SCENARIO_ARCH_DEPS)
        assert any(s.startswith("A3-") for s in stems), (
            f"A3（依赖锁定）在 deps 场景应命中；实测：{stems}"
        )

    def test_arch_A4_hits_deps_scenario(self):
        stems = _hits(SKILL_ROOT / "架构师", SCENARIO_ARCH_DEPS)
        assert any(s.startswith("A4-") for s in stems), (
            f"A4（降级路径）在 deps 场景应命中；实测：{stems}"
        )

    def test_arch_A5_hits_review_scenario(self):
        stems = _hits(SKILL_ROOT / "架构师", SCENARIO_ARCH_REVIEW)
        assert any(s.startswith("A5-") for s in stems), (
            f"A5（评审决策 lockstep）在 review 场景应命中；实测：{stems}"
        )

    def test_frontend_F1_hits_fetch_scenario(self):
        stems = _hits(SKILL_ROOT / "前端工程师", SCENARIO_FRONTEND_FETCH)
        assert any(s.startswith("F1-") for s in stems), (
            f"F1（fetch 响应检查）在 fetch 场景应命中；实测：{stems}"
        )


# ── 层 2：Baseline snapshot regression（回归防线）────────────
class TestBaselineRegression:
    """3 × 3 = 9 场景的命中 skill 集合不能相对 baseline 变化。

    - 无 baseline 或 `UPDATE_SKILL_BASELINE=1` → 首次生成/更新 baseline + skip
    - 任何 skill 集合 diff → fail 强制人工审阅

    审阅确认命中变化符合预期后：
        UPDATE_SKILL_BASELINE=1 pytest .claude/tests/engine/test_skill_trigger_hit_ratio.py::TestBaselineRegression

    baseline 存储：.claude/tests/baselines/skill_trigger.json
    """

    def test_matrix_matches_baseline(self):
        current = _current_matrix()
        baseline = _load_baseline()

        if baseline is None or os.environ.get(UPDATE_ENV_VAR):
            _save_baseline(current)
            action = "首次生成" if baseline is None else "更新"
            pytest.skip(
                f"baseline 已{action}，落盘：{BASELINE_PATH.relative_to(PROJECT_ROOT)}"
            )

        diffs: list[str] = []
        for role, scenarios in ROLE_SCENARIOS:
            for name, _ in scenarios:
                curr_hits = set(current.get(role, {}).get(name, []))
                base_hits = set(baseline.get(role, {}).get(name, []))
                added = curr_hits - base_hits
                removed = base_hits - curr_hits
                if added or removed:
                    parts = [f"  {role} × {name}:"]
                    if added:
                        parts.append(f"    + {sorted(added)}")
                    if removed:
                        parts.append(f"    - {sorted(removed)}")
                    diffs.append("\n".join(parts))

        assert not diffs, (
            f"触发器命中集合相对 baseline 变化（{len(diffs)} 场景）；\n"
            f"审阅 diff 后可用 `{UPDATE_ENV_VAR}=1 pytest ...::TestBaselineRegression` 更新：\n"
            + "\n".join(diffs)
        )
