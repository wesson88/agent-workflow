"""
graph/critic.py — 批判者逐段回路（2026-10-17 视频域 W2；借鉴 FilmWorld"修正无改进即回滚"）

linear step 声明 `review: {by: 批判者, max_rounds: 2, threshold: 80}` 时，角色跑完后：
  1. 批判者读该角色本轮产物 → 输出 `评分: N/100` + 问题单（P0/P1/P2）
  2. 分数 ≥ 阈值 → 结束
  3. 否则角色带问题单重跑（task 追加问题单），批判者再评
  4. 新分 ≤ 旧分 → 回滚到上一版产物并结束；否则继续，最多 max_rounds 轮
每轮评审落 `{项目根}/评审/{角色}-r{k}.md`，可追溯。

批判者不经 role_runner（不产 FILE 块，只出评审文本），直接 call_llm——与讨论节点同一做法；
域视角 adapter（`00-系统/规则/{domain}/批判者-视角.md`）存在则注入。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from ..ability_loader import load_domain_adapter
from ..config import VAULT_ROOT
from ..llm import call_llm
from ..obsidian_io import atomic_write_text
from ..role_invoke import RoleInvocation, invoke_role
from ..role_loader import load_role

_SCORE_RE = re.compile(r"评分\s*[:：]\s*(\d{1,3})")


def _proj_root(domain: str | None, project: str) -> str:
    if domain:
        from ..artifact_registry import load_config
        roots = load_config()
        if domain in roots:
            return roots[domain].replace("{project}", project)
    return f"10-项目/{project}"


def role_output_paths(role_name: str, project: str) -> list[Path]:
    """角色本次产物（不含 {n} 等参数化/通配条目——回路只用于非参数化 linear 步骤）。"""
    out = []
    for rel in load_role(role_name).outputs:
        r = rel.replace("{project}", project)
        if "{" in r or "*" in r:
            continue
        out.append(VAULT_ROOT / r)
    return out


def snapshot(paths: list[Path]) -> dict[Path, str | None]:
    return {p: (p.read_text(encoding="utf-8") if p.is_file() else None) for p in paths}


def restore(snap: dict[Path, str | None]) -> None:
    for p, text in snap.items():
        if text is None:
            if p.exists():
                p.unlink()
        else:
            atomic_write_text(p, text)


def parse_score(text: str) -> int | None:
    m = _SCORE_RE.search(text)
    return min(int(m.group(1)), 100) if m else None


def critique(critic: str, target: str, project: str, domain: str | None,
             docs: dict[Path, str | None], round_no: int) -> tuple[int | None, str]:
    role = load_role(critic)
    adapter, _ = load_domain_adapter(role.name, domain)
    system = (
        f"## 角色摘要\n角色：{role.name}\n风格：{role.style}\n\n{role.body.strip()}{adapter}\n\n"
        "## 本场约束（覆盖默认行为）\n"
        f"你在评审「{target}」本轮产物。不要调用工具、不要写文件、不要输出 FILE 块。\n"
        "输出格式（严格）：\n"
        "第一行：`评分: N/100`（N 为整数；80 及以上表示可交付）\n"
        "然后 `## 问题单`，每条一行：`- [P0|P1|P2] 位置 · 问题 · 改法`（P0=阻断交付，P1=应改，P2=可选）。\n"
        "只评产物本身与它的职责契约，不替角色重写全文。"
    )
    body = "\n\n".join(
        f"### 📄 {p.relative_to(VAULT_ROOT).as_posix()}\n\n{text}" for p, text in docs.items() if text
    ) or "（本轮无产物）"
    user = f"# 待评审：{target}（第 {round_no} 次评审）\n项目：{project}\n\n{body}\n\n---\n请按格式输出评分与问题单："
    text = call_llm(system, user, model=role.model, max_tokens=role.max_tokens,
                    print_stream=False, role_name=role.name)
    return parse_score(text), text


def review_loop(inv: RoleInvocation, review: dict, domain: str | None) -> int:
    """在角色首次成功之后调用。返回最终 returncode（0 = 产物可用，含回滚后的旧版）。"""
    critic = str(review.get("by", "批判者"))
    max_rounds = int(review.get("max_rounds", 2))
    threshold = int(review.get("threshold", 80))
    project, target = inv.project, load_role(inv.role).name
    paths = role_output_paths(target, project)
    note_dir = f"{_proj_root(domain, project)}/评审"

    def judge(k: int) -> tuple[int | None, str]:
        snap = snapshot(paths)
        score, text = critique(critic, target, project, domain, snap, k)
        atomic_write_text(VAULT_ROOT / f"{note_dir}/{target}-r{k}.md",
                          f"---\ntype: critic-review\nrole: {target}\nround: {k}\nscore: {score}\n---\n\n{text}\n")
        print(f"\n🧐 {critic} 评 {target} 第 {k} 次：{score if score is not None else '未给分'}/100")
        return score, text

    score, text = judge(0)
    for k in range(1, max_rounds + 1):
        if score is not None and score >= threshold:
            break
        if score is None:
            print(f"⚠️ {critic} 未按格式给分，回路终止（保留当前产物）")
            break
        prev = snapshot(paths)
        rerun = replace(inv, task=(f"{inv.task}\n\n## 批判者问题单（第 {k} 轮修改，按此逐条处理，"
                                   f"P0/P1 必改）\n\n{text}"))
        if invoke_role(rerun, mode="auto").returncode != 0:
            print(f"⚠️ {target} 第 {k} 轮修改失败，回滚到上一版")
            restore(prev)
            break
        new_score, new_text = judge(k)
        if new_score is None or new_score <= score:
            print(f"↩️  {target} 第 {k} 轮修改未提升（{score} → {new_score}），回滚到上一版")
            restore(prev)
            break
        score, text = new_score, new_text
    return 0
