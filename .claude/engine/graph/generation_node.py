"""
graph/generation_node.py — 视频域执行层节点（2026-10-17 W2）

两种 step：

`type: generation`（单任务，kind = reference / tts / compose）
  - reference：按 视觉设定 frontmatter `references` 出参考图
  - tts：按 配音表单 frontmatter `lines` 出逐句配音 → 实测时长回填报告 → 超镜头时长落人工闸
  - compose：按 剪辑时间线 frontmatter 合成成片 → 实测响度（-14±1 LUFS / TP ≤ -1）不过落人工闸

`type: generation_loop`（逐生成组，组来自 分镜表 frontmatter `groups`）
  每组状态机（台账 `执行/生成台账.md` 持久，重跑从断点继续）：
    keyframe:  prompt（prompt工程师 组调用/回改调用）→ gen → review（样片审阅）
    video:     gen → review
    review 三态：pass → 选定落 assets/selected/ 进下一阶段 / rework → 归档本次候选、次数 +1、
    prompt工程师 回改后重生成（每阶段 ≤ max_attempts）/ degrade 或到上限 → 用降级方案生成一次，
    再不过落人工闸由人决定
  执行层 pending（人工 UI 等用户出图）→ 整个节点 halt；用户放好文件 approve 后重跑接着走。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from ..approval import ask_approval
from ..config import VAULT_ROOT, asset_root
from ..obsidian_io import atomic_write_text, split_frontmatter
from ..role_invoke import RoleInvocation, invoke_role
from ..video_gen import GenTask, run_task
from ..workflow import WorkflowStep
from .state import ProjectState

LOUDNESS_TARGET = (-15.0, -13.0)     # I = -14 ± 1 LUFS
TRUE_PEAK_MAX = -1.0


class GenerationError(RuntimeError):
    pass


# ── vault 读写小工具 ─────────────────────────────────────
def proj_root(domain: str, project: str) -> str:
    from ..artifact_registry import load_config
    return load_config()[domain].replace("{project}", project)


def read_fm(rel: str) -> dict:
    path = VAULT_ROOT / rel
    if not path.is_file():
        raise GenerationError(f"缺少契约文件：{rel}")
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return fm or {}


def write_note(rel: str, fm: dict, body: str) -> None:
    text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body
    atomic_write_text(VAULT_ROOT / rel, text)


def load_groups(domain: str, project: str) -> list[dict]:
    rel = f"{proj_root(domain, project)}/分镜表.md"
    groups = read_fm(rel).get("groups")
    if not isinstance(groups, list) or not groups:
        raise GenerationError(f"{rel} frontmatter 缺 groups（分镜师契约，见注册表 [[分镜表]] 产出指引）")
    for g in groups:
        if not g.get("id") or not g.get("shots"):
            raise GenerationError(f"{rel} groups 条目缺 id/shots：{g}")
    return groups


# ── 媒体实测 ─────────────────────────────────────────────
def duration_s(path: Path) -> float:
    from ..media import _duration_s, _ffmpeg
    _, fp = _ffmpeg()
    return _duration_s(fp, path)


def measure_loudness(path: Path) -> tuple[float, float]:
    """ffmpeg loudnorm 测量（与 bgm-mcp 同口径）→ (integrated LUFS, true peak dBTP)。"""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise GenerationError("响度实测需要 ffmpeg")
    proc = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-i", str(path), "-af",
         "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    text = proc.stderr
    start = text.rfind("{")
    try:
        data = json.loads(text[start:text.rfind("}") + 1])
        return float(data["input_i"]), float(data["input_tp"])
    except (ValueError, KeyError):
        raise GenerationError(f"响度测量失败：{path.name}") from None


# ── 单任务 ───────────────────────────────────────────────
def build_single_task(kind: str, project: str, domain: str) -> GenTask | None:
    root = proj_root(domain, project)
    if kind == "reference":
        rel = f"{root}/视觉设定.md"
        refs = read_fm(rel).get("references") or []
        files = [str(r["file"]) for r in refs if isinstance(r, dict) and r.get("file")]
        if not files:
            raise GenerationError(f"{rel} frontmatter references 为空（美术指导契约）")
        return GenTask("reference", "references", project, domain, tuple(files), (rel,),
                       brief="按视觉设定各节的生成提示出三视图/场景/道具设定图；先锚定风格再出图。")
    if kind == "tts":
        rel = f"{root}/配音表单.md"
        lines = read_fm(rel).get("lines") or []
        if not lines:
            return None   # 无台词项目
        files = [f"assets/voice/{ln['shot']}_{ln['n']}.wav" for ln in lines]
        return GenTask("tts", "voice", project, domain, tuple(files), (rel,),
                       brief="按配音表单逐行 TTS：声线按声线绑定表的 voice_id，情绪按情绪标签；一行一个文件。")
    if kind == "compose":
        rel = f"{root}/剪辑时间线.md"
        fm = read_fm(rel)
        if not fm.get("edl"):
            raise GenerationError(f"{rel} frontmatter 缺 edl（剪辑师契约）")
        expected = ["final/master.mp4"] + (["final/master.srt"] if fm.get("subtitles") else [])
        return GenTask("compose", "master", project, domain, tuple(expected), (rel,),
                       brief="按剪辑时间线 edl 切段拼接、挂配音轨与音乐（ducking）、导出字幕。")
    raise GenerationError(f"generation step 不支持 kind={kind}（单任务只做 reference/tts/compose）")


def check_voice(project: str, domain: str) -> tuple[list[str], str]:
    """配音实测时长 vs 镜头时长 → (超时行, 报告正文)。"""
    root = proj_root(domain, project)
    lines = read_fm(f"{root}/配音表单.md").get("lines") or []
    shot_dur = {s["id"]: float(s["dur"]) for g in load_groups(domain, project) for s in g["shots"]}
    aroot = asset_root(domain, project)
    rows, over = ["| 镜号 | 行 | 偏移 | 实测 | 镜头时长 | 结论 |", "|---|---|---|---|---|---|"], []
    for ln in lines:
        d = duration_s(aroot / f"assets/voice/{ln['shot']}_{ln['n']}.wav")
        end = float(ln.get("offset", 0)) + d
        limit = shot_dur.get(ln["shot"])
        ok = limit is None or end <= limit + 1e-6
        if not ok:
            over.append(f"{ln['shot']}_{ln['n']}（{end:.2f}s > {limit:.2f}s）")
        rows.append(f"| {ln['shot']} | {ln['n']} | {ln.get('offset', 0)} | {d:.2f}s | "
                    f"{limit if limit is not None else '?'} | {'✅' if ok else '❌ 超时'} |")
    return over, "\n".join(rows) + "\n"


def make_generation_node(step: WorkflowStep, halt_on_failure: bool, domain: str | None = None):
    kind = str(step.extras.get("kind", ""))
    name = step.name or step.extras.get("name") or f"生成:{kind}"

    def fail(msg: str) -> dict:
        print(f"\n❌ {name}：{msg}")
        patch = {"failed": [name]}
        if halt_on_failure:
            patch["halted"] = True
        return patch

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            return {"skipped": [name]}
        project = state["project"]
        if not domain:
            return fail("工作流未声明 domain")
        try:
            task = build_single_task(kind, project, domain)
            if task is None:
                print(f"\n⏭️  {name}：无需生成（无台词）")
                return {"succeeded": [name]}
            res = run_task(task)
            if res.status == "pending":
                print(f"\n⏸  {name}：{res.detail}")
                return {"halted": True, "skipped": [name]}
            if res.status == "failed":
                return fail(res.detail)
            if kind == "tts":
                over, table = check_voice(project, domain)
                root = proj_root(domain, project)
                write_note(f"{root}/执行/配音实测时长.md", {"type": "generation-report", "overflow": over},
                           f"# 配音实测时长\n\n{table}")
                if over:
                    d = ask_approval(project, "voice_overflow",
                                     f"配音超出镜头时长：{over}——回提分镜师调时长/拆镜（删分镜表后重跑），或 approve 接受",
                                     node=name, refs=[f"{root}/执行/配音实测时长.md"])
                    if d == "pending":
                        return {"halted": True, "skipped": [name]}
                    if d == "rejected":
                        return fail(f"配音超时被驳回：{over}")
            if kind == "compose":
                lufs, tp = measure_loudness(asset_root(domain, project) / "final/master.mp4")
                ok = LOUDNESS_TARGET[0] <= lufs <= LOUDNESS_TARGET[1] and tp <= TRUE_PEAK_MAX
                print(f"\n🔊 成片响度 I={lufs:.2f} LUFS / TP={tp:.2f} dBTP（{'达标' if ok else '不达标'}）")
                if not ok:
                    d = ask_approval(project, "loudness",
                                     f"成片响度 I={lufs:.2f} LUFS / TP={tp:.2f} 不在 -14±1 / ≤-1——重做母带或 approve 接受",
                                     node=name)
                    if d == "pending":
                        return {"halted": True, "skipped": [name]}
                    if d == "rejected":
                        return fail("成片响度不达标被驳回")
        except GenerationError as e:
            return fail(str(e))
        print(f"\n✅ {name} 完成")
        return {"succeeded": [name]}

    node.__name__ = f"node_generation_{kind}"
    return node


# ── 生成循环 ─────────────────────────────────────────────
PROMPT_ROLE = "prompt工程师"
REVIEW_ROLE = "样片审阅"


class _Halt(Exception):
    pass


class GroupRunner:
    """单个生成组的状态机。台账条目：{phase, stage, attempt, mode, status}。"""

    def __init__(self, project: str, domain: str, task: str, group: dict, entry: dict, max_attempts: int):
        self.project, self.domain, self.task = project, domain, task
        self.g, self.e, self.max = group, entry, max_attempts
        self.gid = str(group["id"])
        self.root = proj_root(domain, project)
        self.aroot = asset_root(domain, project)

    # 状态推进到终态或抛 _Halt / GenerationError
    def run(self) -> None:
        e = self.e
        while e["status"] not in ("done", "degraded"):
            if e["stage"] == "prompt":
                self._prompt()
            elif e["stage"] == "gen":
                self._gen()
            elif e["stage"] == "review":
                self._review()
            elif e["stage"] == "escalate":
                self._escalate()
            else:
                raise GenerationError(f"{self.gid} 台账 stage 非法：{e['stage']}")

    def _invoke(self, role: str, task: str) -> None:
        res = invoke_role(RoleInvocation(role=role, task=task, project=self.project,
                                         module_id=self.gid, domain=self.domain), mode="auto")
        if res.returncode != 0:
            raise GenerationError(f"{role}（{self.gid}）失败：{res.error or res.returncode}")

    def _prompt(self) -> None:
        e = self.e
        if e["attempt"] == 1 and e["phase"] == "keyframe":
            task = f"组调用：为生成组 {self.gid} 产 image/video 两个 prompt 包。"
        else:
            task = (f"回改调用：生成组 {self.gid} 的{'关键帧' if e['phase'] == 'keyframe' else '视频'}"
                    f"第 {e['attempt'] - 1} 次未过审，按 `审阅/{self.gid}.审阅.md` 的失败标签与回改建议定点改写"
                    f"（只改失败镜，版本号 +1，写明改动）。")
        self._invoke(PROMPT_ROLE, task)
        e["stage"] = "gen"

    def _task(self) -> GenTask:
        e, gid = self.e, self.gid
        shots = [s["id"] for s in self.g["shots"]]
        packs = (f"{self.root}/prompt/{gid}/{gid}.image.md", f"{self.root}/prompt/{gid}/{gid}.video.md")
        params = {"mode": e["mode"]} if e["mode"] != "normal" else {}
        if e["phase"] == "keyframe":
            exp = tuple(f"assets/images/{gid}/{s}_img_*.png" for s in shots)
            brief = "按 image 包逐镜出关键帧（每镜 2-4 张候选）。"
        else:
            exp = (f"assets/videos/{gid}/{gid}_vid_*.mp4",)
            brief = ("按 video 包用 assets/selected/ 下本组关键帧作首帧生成（多镜组按时间码分段一次生成；"
                     "单镜失败用单镜备用 prompt）。")
            params["seconds"] = sum(float(s["dur"]) for s in self.g["shots"]) + 1
            if str(self.g.get("lipsync", "无")) == "生成时":
                params["lipsync"] = "生成时：挂 assets/voice/ 本组镜头的配音驱动口型"
        if e["mode"] == "degrade":
            brief = "**降级方案**：按 prompt 包的「降级方案」节生成（远景/背影/反应镜/空镜），不再用原画面。"
        return GenTask(e["phase"], gid, self.project, self.domain, exp, packs,
                       attempt=e["attempt"], brief=brief, params=params)

    def _gen(self) -> None:
        res = run_task(self._task())
        if res.status == "pending":
            print(f"\n⏸  {self.gid} {self.e['phase']} 第 {self.e['attempt']} 次：{res.detail}")
            raise _Halt
        if res.status == "failed":
            raise GenerationError(f"{self.gid} 执行失败：{res.detail}")
        self.e["stage"] = "review"

    def _review(self) -> None:
        e = self.e
        label = "关键帧" if e["phase"] == "keyframe" else "视频 take"
        self._invoke(REVIEW_ROLE, f"生成审阅：生成组 {self.gid} 的{label}（第 {e['attempt']} 次"
                                  f"{'，降级方案' if e['mode'] == 'degrade' else ''}）。frontmatter phase 写 {e['phase']}。")
        fm = read_fm(f"{self.root}/审阅/{self.gid}.审阅.md")
        if str(fm.get("phase")) != e["phase"]:
            raise GenerationError(f"{self.gid} 审阅单 phase={fm.get('phase')}，期望 {e['phase']}（样片审阅契约）")
        verdict = str(fm.get("verdict", ""))
        print(f"\n🔎 {self.gid} {label} 第 {e['attempt']} 次审阅：{verdict}")
        if verdict == "pass":
            self._select(fm.get("selected") or [])
            if e["phase"] == "keyframe":
                e.update(phase="video", stage="gen", attempt=1, mode="normal")
            else:
                e.update(status="degraded" if e["mode"] == "degrade" else "done", stage="done")
            return
        if verdict not in ("rework", "degrade"):
            raise GenerationError(f"{self.gid} 审阅单 verdict={verdict!r} 非法（pass/rework/degrade）")
        self._archive()
        if e["mode"] == "degrade":
            e["stage"] = "escalate"
        elif verdict == "degrade" or e["attempt"] >= self.max:
            e.update(mode="degrade", attempt=e["attempt"] + 1, stage="gen")
        else:
            e.update(attempt=e["attempt"] + 1, stage="prompt")

    def _escalate(self) -> None:
        """降级方案也没过：交给人。approve = 人已手动处理好 assets/selected/ 本组素材。"""
        d = ask_approval(self.project, f"generation_escalate:{self.gid}:{self.e['phase']}",
                         f"生成组 {self.gid} {self.e['phase']} 降级方案仍未过审——请手动处理"
                         f"（放好 assets/selected/ 本组素材）后 approve",
                         node="generation_loop", refs=[f"{self.root}/审阅/{self.gid}.审阅.md"])
        if d == "pending":
            raise _Halt
        if d == "rejected":
            raise GenerationError(f"{self.gid} 降级后仍被驳回")
        if self.e["phase"] == "keyframe":
            self.e.update(phase="video", stage="gen", attempt=1, mode="normal")
        else:
            self.e.update(status="degraded", stage="done")

    def _select(self, selected: list) -> None:
        if not selected:
            raise GenerationError(f"{self.gid} 审阅 pass 但 selected 为空（样片审阅契约）")
        dst = self.aroot / "assets" / "selected"
        dst.mkdir(parents=True, exist_ok=True)
        takes: dict[str, str] = {}
        for s in selected:
            src = self.aroot / str(s["file"])
            if not src.is_file():
                raise GenerationError(f"{self.gid} 选定文件不存在：{s['file']}")
            if self.e["phase"] == "keyframe":
                shutil.copy2(src, dst / f"{s['shot']}_key{src.suffix}")
            elif str(s["file"]) not in takes:
                takes[str(s["file"])] = f"{self.gid}_take{len(takes) + 1}{src.suffix}"
                shutil.copy2(src, dst / takes[str(s["file"])])

    def _archive(self) -> None:
        sub = "images" if self.e["phase"] == "keyframe" else "videos"
        d = self.aroot / "assets" / sub / self.gid
        if not d.is_dir():
            return
        arch = d / "_archive" / f"a{self.e['attempt']}"
        arch.mkdir(parents=True, exist_ok=True)
        for p in d.iterdir():
            if p.is_file():
                shutil.move(str(p), arch / p.name)


def ledger_rel(domain: str, project: str) -> str:
    return f"{proj_root(domain, project)}/执行/生成台账.md"


def _render_ledger(ledger: dict) -> str:
    rows = ["| 组 | 阶段 | 环节 | 次数 | 模式 | 状态 |", "|---|---|---|---|---|---|"]
    for gid, e in ledger.items():
        rows.append(f"| {gid} | {e['phase']} | {e['stage']} | {e['attempt']} | {e['mode']} | {e['status']} |")
    return "# 生成台账\n\n引擎维护（generation_loop），人工勿改；删除本文件 = 全部组从头重跑。\n\n" + "\n".join(rows) + "\n"


def make_generation_loop_node(step: WorkflowStep, halt_on_failure: bool, domain: str | None = None):
    name = step.name or step.extras.get("name") or "生成循环"
    max_attempts = int(step.extras.get("max_attempts", 3))

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            return {"skipped": [name]}
        project = state["project"]

        def fail(msg: str) -> dict:
            print(f"\n❌ {name}：{msg}")
            patch = {"failed": [name]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        if not domain:
            return fail("工作流未声明 domain")
        try:
            groups = load_groups(domain, project)
            lrel = ledger_rel(domain, project)
            ledger = (read_fm(lrel).get("groups") or {}) if (VAULT_ROOT / lrel).is_file() else {}
            for g in groups:
                gid = str(g["id"])
                entry = ledger.setdefault(gid, {"phase": "keyframe", "stage": "prompt", "attempt": 1,
                                                "mode": "normal", "status": "running"})
                try:
                    GroupRunner(project, domain, state["task"], g, entry, max_attempts).run()
                finally:
                    write_note(lrel, {"type": "generation-ledger", "groups": ledger}, _render_ledger(ledger))
        except _Halt:
            return {"halted": True, "skipped": [name]}
        except GenerationError as e:
            return fail(str(e))
        n_deg = sum(1 for e in ledger.values() if e["status"] == "degraded")
        print(f"\n✅ {name} 完成：{len(groups)} 组（降级 {n_deg}）")
        return {"succeeded": [name]}

    node.__name__ = "node_generation_loop"
    return node
