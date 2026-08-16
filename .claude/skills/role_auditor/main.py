"""
role_auditor/main.py — 角色审计器执行入口

作用：
  对照 vault `00-系统/规则/角色基因规范.md` 审计所有 `角色-*.md` 文件，
  输出可操作的偏离清单到 `00-系统/审计报告/角色基因审计-{date}.md`。

  程序层先做可量化测量（字符长度 / frontmatter 字段 / DYNAMIC regex），
  把测量结果连同规范 + 所有角色全文传给 LLM，LLM 负责语义判断（反模式 / 豁免）
  并产出最终报告。

  历史称呼：2026-06-10 前称"角色规范师"，对齐 engine `role_auditor` 改名为
  "角色审计器"；vault 角色基因 frontmatter aliases 保留旧名兼容历史复盘文档。

CLI：
  python .claude/skills/role_auditor/main.py [--dry-run] [--target X [--target Y]]
    --dry-run    只打印测量结果，不调 LLM、不写盘
    --target     治理对象选择（可重复 / 逗号分隔 / "all"）
                 - 不传 = 全部角色（除审计者本身）
                 - --target 后端工程师 = 单个
                 - --target 后端,前端 = 多个
"""

from __future__ import annotations

import argparse
import re
import sys
import yaml
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, parse_targets,
    # 私有名，但**必须**共用：审计器量的必须是引擎真正注入的那段文本。
    # 若在此另写一份抽取逻辑，两边一漂移审计就再次失真 —— 2026-08-16 前的
    # 口径错配（把不进 prompt 的 §7/§8 算进正文，4 假阳性）就是这么来的。
    # 参 [[feedback_contract_three_layers]]：同一契约不允许两份实现。
    _extract_role_prompt_sections,
)
from engine import (
    set_role_status, role_is_blocked,
    VAULT_ROOT, role_genes_dir, resolve_path,
)

ROLE = "角色审计器"

# 不审计自身
SELF_FILENAME = "角色-角色审计器.md"

# 规范文档路径（vault 相对）
SPEC_REL = "00-系统/规则/角色基因规范.md"

# 报告输出目录
AUDIT_DIR_REL = "00-系统/审计报告"

# frontmatter 必填字段（来自规范 §2.1 / §2.2）
REQUIRED_FIELDS = {
    "role", "domain", "model", "max_tokens", "style",
    "aliases", "upstream", "downstream", "monitors",
    "inputs", "outputs", "tools",
}

# 规范 §2.2 声明为 list[str] 的字段。"值可为空但必须列出" —— 空写 `[]`，不写 YAML `null`。
# `null` 反序列化成 Python None，任何迭代它的关系图构建 / load_role 路径都会抛
# TypeError，而"缺必填"检测把 null 视为"字段存在"因此不报警（2026-08-13 审计在
# 产品经理 upstream: null 上实证了这个盲区）。
LIST_FIELDS = {
    "aliases", "upstream", "downstream", "monitors",
    "inputs", "outputs", "tools", "skills", "skill_refs",
}

# frontmatter 禁止字段（来自规范 §2.4）
FORBIDDEN_FIELDS = {
    "responsibilities", "职责", "forbidden", "禁止事项",
    "workflow", "description", "prompt_template",
}

# 长度上限（字符数）+ 数量上限
# ⚠️ 阈值来源硬约束（见项目 CLAUDE.md「阈值来源必须显式声明」）
LIMITS = {
    # 2026-08-15 校准：800 → 2000。
    # 依据：**实测**。原 800 是「初值，无数据支持」，且注释里按 ~3.5 chars/token
    # 换算，说明当初是**当 token 预算来定的** —— 这是概念错配：frontmatter 绝大
    # 部分根本不进 prompt。全量 27 个角色实测：
    #   frontmatter 合计 24,090 chars，真正进 LLM 的仅 3,158 chars（13%）
    # 且进 prompt 的量与 frontmatter 总长几乎无关 —— 非契约化角色恒定 62-97 chars
    # （prompt_builder.build_system_prompt 的「角色摘要」只取 role/domain/style/
    # skills 四项）；契约化角色多出 outputs/inputs 清单（common._render_contract_
    # summary），最大 前端工程师 372 chars ≈ 106 tokens。其余字段（aliases/upstream/
    # downstream/monitors/tools/version/max_tokens/model/produces/consumes/
    # budget_input_tokens/domains）只在引擎内部消费，零 prompt 成本。
    # 结论：本阈值衡量的是**可维护性**不是 token 成本，800 定得过紧（13/27 超限，
    # 即 48% 失守，阈值已失去信号价值）。2000 后仅 前端工程师(2273) 仍超限 —— 它
    # 的膨胀源是 output_contract/input_contract 模板，属于真该治理的对象，保留信号。
    # 复盘见 vault [[Obsidian可视化仪表盘建设-2026-08-15]]。
    "frontmatter": 2000,
    # ── 以下四项 2026-08-16 补依据 + 修口径 ────────────────────────────
    # 背景：这四个值同生于 66c0cb1（2026-05-10），当时 LIMITS 上方只有一行
    # `# 长度上限（字符数）`，**五个值全裸无依据**。vault `角色基因规范.md` §4
    # 仅为两个 5000 写了「≈ 1400 tokens，配合上下文注入仍在合理范围」——
    # 「合理范围」正是项目 CLAUDE.md 反例清单点名的无效依据；且 5000/1400
    # ≈ 3.57 chars/token 与 frontmatter 800 那条同源，而后者已被证伪为概念错配。
    # 佐证该批值属 a priori 拍脑袋：66c0cb1 自带的首轮审计基线即写着
    # 「后端工程师正文 11377 chars（规范上限 5000）」—— 先定死再去量，一量就 2.3 倍。
    #
    # 本轮全量实测 26 角色重定依据。**关键发现：口径错了，值没错**。
    #
    # `prompt_body`（原 `body_no_dynamic`，改名因旧名描述的就是错的量）：
    #   旧口径 = 整个 body 减 DYNAMIC，**把 §7/§8 也算进去**；而业务角色走
    #   `common._extract_role_prompt_sections` 严格 §1-§6，§8 版本历史一个字
    #   不进 prompt。实测 23 个业务角色：旧口径合计 87264 chars，真进 prompt
    #   61838，**虚高 29%**（技术主管虚高 48%：6683 计入 / 3491 实注入）。
    #   后果：旧口径报 4 个超限（技术主管/前端/后端/架构师），按真实注入量
    #   **0 个超限** —— 4 假阳性 / 0 真阳性，指标已完全失真。
    #   更反常的是它**惩罚写文档**：本轮给技术主管补 v1.8.0 版本历史（950 chars，
    #   记录治理依据，正是 CLAUDE.md 硬性要求）后，旧指标从 5733 涨到 6683。
    #   依据：**实测断层**。改口径后 26 角色注入量 max=5269（复盘者，元角色走
    #   meta_full 路径）、次高 4174（后端工程师），断层宽 1095；5000 落在
    #   (4174, 5269) 内，命中 1/26。中位 2636 / 均值 2761 / min 1142。
    "prompt_body": 5000,
    # `single_section` 同步改口径：只量注入范围内的章节（业务 §1-§6 / 元角色
    #   全 body 减 DYNAMIC 与版本历史），不再把 §8 版本历史算成"最大章节"。
    #   依据：**实测断层**。改口径后 158 个章节：超 1500 的 6 个（复盘者 §3 2388 /
    #   后端 §6 2052 / 前端 §6 1878 / 架构师 §6 1763 / 创意记录员 §3 1618 /
    #   技术主管 §5 1537），其下一档是 1173（知识沉淀者 §4）；断层 (1173, 1537)
    #   宽 364，1500 落在其中，命中率 6/158 = 3.8%。中位 306。
    "single_section": 1500,
    # `dynamic`：DYNAMIC 区确实进 prompt（按 [KEEP]/[GRADUATE?] label 过滤后
    #   注入，见 P10.5 B4），故 token 口径本身成立，无需改。
    #   依据：**实测上界 + 明标未校准**。26 角色实测 max=2037（后端工程师，
    #   仅为限额 41%）、中位 99、**超限 0 个**。即本阈值当前无实战命中，
    #   5000 属沿用 66c0cb1 的未校准上界，保留作堆积护栏。
    #   ⚠️ 待补丁囤积真发生过 ≥ 1 次后按实测重定（已挂 98-待办）。
    "dynamic": 5000,
    # `single_patch` 依据：**实测断层**。25 条 patch：超 1200 的 2 条
    #   （制作人 #1 1449 / 作曲 #1 1278），其下一档 984（后端 #2）；
    #   断层 (984, 1278) 宽 294，1200 落在其中，命中 2/25 = 8%。
    #   分布双峰：21 条 ≤ 125（多为 125 字样板头），其余 700-1449。
    "single_patch": 1200,
    # 2026-08-13 新增：业务角色 §1-§6 单章节最小有效正文（剥离 HTML 注释与空白后）
    # 依据：**实测断层**。2026-08-13 全量测了本库 22 个业务角色的 §1-§6 有效字符
    # （剥离 HTML 注释与全部空白后）：已知空壳章节最大 32（三个空壳角色的
    # §5「参见 frontmatter inputs / outputs。」），真实内容章节最小 51
    # （批判者 §6）。40 落在 (32, 51) 断层正中，两侧各留 8 chars 余量。
    # ⚠️ 口径必须是「剥离空白后」——若按原始字符计，两类分布重叠，无阈值可分。
    # 缘由：2026-08-13 审计发现 A&R / MIDI编程 / 录音师 三个角色的 §1/§3/§4/§6
    # 正文全是 `<!-- W2-W3 起草 -->` HTML 注释，实质内容为零，但因章节标题存在，
    # _extract_role_prompt_sections 不 raise、T2.7 lint 全绿 —— 空壳角色若被调度，
    # 注入的是一个无使命、无职责、无边界的自由裁量 agent。
    "min_section_chars": 40,
    # P6 新增：角色 skill_refs 数量软上限（触发 [SHRINK?]）
    # 依据：**推导逻辑 + 实测参考**。当前实测：架构师 5 skill / 后端 4 / TL 2 / 前端 1，
    # 上限 = 现最高值（架构师 5），高于此值提示治理域过宽应拆分。
    # 命中不阻塞 load_role，仅 LLM 报告标记。等音乐域 9+ 角色实战后再评估上调。
    "skill_refs_max": 5,
}


# P6 canonical skill 触发器 schema（对齐 skill_trigger.py::match_skill 读取契约）
# 一个 skill 文件的 frontmatter 至少要满足以下三条之一，否则 fail-closed 不召回：
#   - trigger.keywords ≥ 1 项
#   - trigger.file_patterns ≥ 1 项
#   - trigger.always: true
def _skill_trigger_valid(skill_fm: dict) -> bool:
    """判断 skill frontmatter 的 trigger 字段是否合法（能被 skill_trigger 召回）。

    对齐 `.claude/engine/skill_trigger.py::match_skill` 的读取逻辑；命名规范
    见 [[角色基因规范#§11]] / [[capability注册表机制-立项-2026-07-02#§11.4]]。
    """
    trigger = skill_fm.get("trigger") if isinstance(skill_fm, dict) else None
    if not isinstance(trigger, dict):
        return False
    if trigger.get("always") is True:
        return True
    keywords = trigger.get("keywords")
    if isinstance(keywords, list) and any(isinstance(k, str) and k.strip() for k in keywords):
        return True
    file_patterns = trigger.get("file_patterns")
    if isinstance(file_patterns, list) and any(
        isinstance(p, str) and p.strip() for p in file_patterns
    ):
        return True
    return False


# PM 角色 PRD.md 越界 pattern（来源：[[PM越界-PRD写下游内容]]）
# 命中表示 PM 越界写了应属架构师 / TL 的内容（schema / API 表 / 框架推荐 / 任务拆分）。
# pattern 设计取舍：宁可短期误报（如「待确认项」里的「Vue 还是 React」），命中后人工
# review；不放过实际越界。下个新项目实战后再精化。
PM_OVERFLOW_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("api_table_header",
     r"\|\s*方法\s*\|\s*路径\s*\|",
     "API endpoint 表头"),
    ("api_method_row",
     r"\|\s*(GET|POST|PUT|DELETE|PATCH)\s*\|\s*`?/[\w/{}:?=&_\-\.]+`?\s*\|",
     "API 方法/路径表格行（method | path 两栏）"),
    ("ddl_field",
     r"(INTEGER\s+PK|TEXT\s+NOT\s+NULL\s+UNIQUE|TEXT\s+NOT\s+NULL|REAL\s+NOT\s+NULL|VARCHAR\(\d+\))",
     "DDL 字段类型"),
    ("schema_table_header",
     r"\|\s*字段\s*\|\s*类型\s*\|",
     "schema 字段表头"),
    ("framework_choice",
     r"(Flask\s*(vs|/|或)\s*FastAPI|FastAPI\s*(vs|/|或)\s*Flask|React\s*(vs|/|或)\s*Vue|Vue\s*(vs|/|或)\s*React|Chart\.js\s*(vs|/|或)\s*ECharts)",
     "框架选型推荐"),
    ("task_split_header",
     r"\|\s*#?\s*\|\s*任务\s*\|\s*角色\s*\|",
     "任务拆分表头（# / 任务 / 角色）"),
    ("task_id_row",
     r"^\|\s*T\d+[a-z]?\s*\|.+\|\s*(后端|前端|架构)[\w\s\-]*\|",
     "T<n> 任务分派表格行"),
)


def _detect_pm_overflow(prd_path: Path) -> list[dict]:
    """正则扫 PRD.md，返回命中越界 pattern 的 hit 列表。

    每条 hit：{pattern_id, desc, line, snippet}。
    无 PRD.md（PM 尚未跑）或读取失败 → 返回空列表，不视为错误。
    """
    if not prd_path.is_file():
        return []
    try:
        content = prd_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[{ROLE}] ⚠️ 读 {prd_path.name} 失败：{e}", file=sys.stderr)
        return []

    hits: list[dict] = []
    lines = content.splitlines()
    for pattern_id, regex, desc in PM_OVERFLOW_PATTERNS:
        pat = re.compile(regex, re.MULTILINE)
        for m in pat.finditer(content):
            # 算行号：从 match 起始位置反推
            line_no = content[: m.start()].count("\n") + 1
            snippet = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            hits.append({
                "pattern_id": pattern_id,
                "desc": desc,
                "line": line_no,
                "snippet": snippet[:200],
            })
    return hits


def _run_pm_output_audit(*, dry_run: bool = False) -> int:
    """扫 vault 下所有 10-项目/*/PRD.md，命中越界即写 audit.jsonl + 递增 PM consecutive_failures。

    返回值（CLI 状态码）：
      0 — 所有 PRD 合规，无命中
      0 — 命中但非 dry_run，已记录（state 已递增）
      0 — dry_run，仅打印不写盘
      2 — 没有可审计的 PRD（vault 下 10-项目 为空）
    """
    projects_root = VAULT_ROOT / "10-项目"
    if not projects_root.is_dir():
        print(f"[{ROLE}] vault {projects_root} 不存在，跳过产物审计", file=sys.stderr)
        return 2

    prd_paths = sorted(projects_root.glob("*/PRD.md"))
    if not prd_paths:
        print(f"[{ROLE}] 未在 {projects_root} 下找到任何 PRD.md", file=sys.stderr)
        return 2

    overflow_count = 0
    total_hits = 0
    for prd in prd_paths:
        project = prd.parent.name
        hits = _detect_pm_overflow(prd)
        if not hits:
            print(f"[{ROLE}] ✅ {project}/PRD.md 合规（无越界 pattern 命中）")
            continue

        overflow_count += 1
        total_hits += len(hits)
        pattern_ids = sorted({h["pattern_id"] for h in hits})
        print(
            f"[{ROLE}] ⚠️ {project}/PRD.md 命中 {len(hits)} 个越界（{len(pattern_ids)} 类）：" + ", ".join(pattern_ids),
            file=sys.stderr,
        )
        for h in hits[:5]:
            print(f"    line {h['line']}  [{h['pattern_id']}]  {h['snippet']}", file=sys.stderr)
        if len(hits) > 5:
            print(f"    …（还有 {len(hits) - 5} 条，详见 audit.jsonl）", file=sys.stderr)

        if dry_run:
            continue

        # 写 audit.jsonl
        try:
            rel = prd.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            rel = str(prd)
        append_audit({
            "timestamp": utc_now(),
            "type": "pm_output_overflow",
            "role": "产品经理",
            "project": project,
            "prd_path": rel,
            "hit_count": len(hits),
            "patterns": pattern_ids,
            "hits": hits,
            "audited_by": ROLE,
        })

        # 递增 PM consecutive_failures（一次跑一次性 +1，不按 pattern 数累加）
        set_role_status(
            "产品经理",
            increment_consecutive_failures=True,
            enforce_transition=False,
        )

    print(
        f"[{ROLE}] 产物审计完成：扫 {len(prd_paths)} 个 PRD，"
        f"{overflow_count} 个越界，共 {total_hits} 处命中"
        + ("（dry_run，未写盘）" if dry_run else "")
    )
    return 0

_DYNAMIC_RE = re.compile(
    r"<!-- DYNAMIC_START -->(.*?)<!-- DYNAMIC_END -->",
    re.DOTALL,
)

_PATCH_HEADER_RE = re.compile(
    r"^#\s*Patch\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*(.+?)\s*$",
    re.MULTILINE,
)

# 章节标题 regex（§1-§8）
_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+", re.MULTILINE)

# vault stem 唯一性扫描排除路径前缀（与 engine.wikilink.resolve_target 的索引对齐）
# - 10-项目/<proj>/：项目产出 PRD/系统设计 等多项目同名是常态，命名规则豁免
# - 99-临时/：临时区不参与 wikilink，按 vault命名规则.md §2.9 豁免
# - .runtime-state/：运行时状态文件，不是 vault 笔记
_STEM_SCAN_EXCLUDES = ("10-项目/", "99-临时/", ".runtime-state/")


def _is_domain_rule_adapter(rel_posix: str) -> bool:
    """跨域适配器路径模板：`00-系统/规则/<domain>/<adapter>.md`（vault命名规则 §2.11）。

    与 engine.wikilink._is_domain_rule_adapter 同步：domain 子目录下的同名 stem
    是开闭原则的设计意图，stem 扫描应跳过。
    """
    parts = rel_posix.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "00-系统"
        and parts[1] == "规则"
    )


def _parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """从 markdown 文件提取 frontmatter。

    返回 (frontmatter_dict, body_text, frontmatter_char_count)。
    frontmatter_dict 为空表示无 frontmatter。
    """
    if not text.startswith("---"):
        return {}, text, 0
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text, 0
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except Exception:
        fm = {}
    return fm, body, len(text[:end + 4])


# 规范 §11 契约化字段。这两个键不计入 frontmatter 软上限 —— 见 _split_fm_contract。
_CONTRACT_KEYS = frozenset({"output_contract", "input_contract"})
_FM_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


def _split_fm_contract(fm_raw: str) -> tuple[str, str]:
    """把 frontmatter 原文拆成 (计入软上限的部分, 契约字段部分)。

    2026-08-15 新增。成因：规范 §11 鼓励 output_contract / input_contract 契约化，
    但 §4 的 frontmatter 软上限把契约模板一并计入 —— **越遵循 §11 的角色越必然违反
    §4**（2026-08-13 审计 [[角色基因劣化对比-2026-08-13]] §2.2「越合规越超标」已定性）。

    实测：契约字段占 前端工程师 1281 / 技术主管 968 / 后端工程师 994 chars，
    而其余 24 个角色根本没有这两个键 —— 排除后 fm_chars 一字不变，零影响。

    软上限的治理目标是「字段值不要塞长描述」；契约模板是结构化声明、由 workflow
    在运行时按 contract_overrides 实例化，与该目标无关，故拆出单独计数。

    切分口径：frontmatter 是行式 YAML，顶层键在列 0，缩进行 / `- ` 行归属当前键。
    """
    blocks: list[tuple[str, str]] = []
    cur_key: str | None = None
    cur: list[str] = []
    for line in fm_raw.split("\n"):
        m = _FM_TOP_KEY_RE.match(line)
        if m:
            if cur_key is not None:
                blocks.append((cur_key, "\n".join(cur)))
            cur_key, cur = m.group(1), [line]
        elif cur_key is not None:
            cur.append(line)
        else:
            # 首个顶层键之前的内容（正常不该有），归入计数部分
            blocks.append(("", line))
    if cur_key is not None:
        blocks.append((cur_key, "\n".join(cur)))

    kept = [b for k, b in blocks if k not in _CONTRACT_KEYS]
    dropped = [b for k, b in blocks if k in _CONTRACT_KEYS]
    return "\n".join(kept), "\n".join(dropped)


def _last_dynamic_body(text: str) -> str:
    ms = list(_DYNAMIC_RE.finditer(text))
    return ms[-1].group(1) if ms else ""


def _split_sections(body: str) -> dict[str, list[str]]:
    """切 §1-§N 章节，返回 {章节号: 该章内容行列表}（不含标题行本身）。

    **跳过 fenced code block**：角色正文里常内嵌产物 markdown 模板，模板自身
    也用 `## 1.` / `## 2.` 当标题（如创意发散者 §3 内嵌「创意发散-R{n}.md」模板）。
    不跳围栏的话，模板里的 `## 1.` 会被当成外层章节，把真正的 §1 内容整个覆盖掉
    —— 创意发散者 §1 实测 180 chars 被误记为 21 chars。
    2026-08-13 修复；此前所有涉及内嵌模板角色的章节长度测量都是错的。
    """
    sections: dict[str, list[str]] = {}
    current = None
    in_fence = False
    for line in body.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if current is not None:
                sections[current].append(line)
            continue
        if in_fence:
            if current is not None:
                sections[current].append(line)
            continue
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _section_char_counts(body: str) -> dict[str, int]:
    """切 §1-§N 章节，返回每章字符数（不含标题行本身）。"""
    return {k: len("\n".join(v)) for k, v in _split_sections(body).items()}


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _section_effective_chars(body: str) -> dict[str, int]:
    """切 §1-§N 章节，返回每章**剥离 HTML 注释与空白后**的有效正文字符数。

    与 _section_char_counts 的区别：那个量的是原始体积（用于超上限判定），
    这个量的是"LLM 真正读到多少内容"（用于空壳判定）。
    HTML 注释不进渲染正文，也不进 system prompt —— 一章只有 `<!-- 待起草 -->`
    等价于该章缺失。
    """
    out: dict[str, int] = {}
    for k, v in _split_sections(body).items():
        text = _HTML_COMMENT_RE.sub("", "\n".join(v))
        out[k] = len("".join(text.split()))
    return out


def _max_version_in_section8(body: str) -> str | None:
    """取 §8 版本历史里全部版本号的 semver 最大值。

    全库 §8 有表格 / bullet 两种格式，且历史上升序降序并存（规范 §3.4a 已统一为
    降序，但校验按"取最大值"判定，与格式和排列方向都无关，向后兼容旧文件）。
    """
    m8 = re.search(r"^##\s+8\.", body, re.M)
    if not m8:
        return None
    sec = body[m8.end():]
    found = (re.findall(r"^\|\s*(\d+\.\d+\.\d+)\s*\|", sec, re.M)
             + re.findall(r"^-\s*\*?\*?v(\d+\.\d+\.\d+)", sec, re.M))
    if not found:
        return None
    return max(found, key=lambda v: tuple(int(x) for x in v.split(".")))


def _split_patches(dynamic_body: str) -> list[str]:
    """把 DYNAMIC 区域按 '# Patch' 行切成独立补丁块。"""
    chunks: list[str] = []
    buf: list[str] = []
    for line in dynamic_body.split("\n"):
        if line.strip().startswith("# Patch"):
            if buf:
                chunks.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if c.strip()]


def _check_dynamic_marker_literal(body_no_dynamic: str) -> bool:
    """检查正文（不含 DYNAMIC 区域本身）是否字面引用了 DYNAMIC_START marker。

    规范 §6.4：字面引用（包括反引号包裹的 inline code）会破坏 regex 解析。
    """
    # 用 `` ` `` 包裹或直接出现的 DYNAMIC_START（但不是在 DYNAMIC 区域注释行里）
    return bool(re.search(r"`?<!--\s*DYNAMIC_START\s*-->`?", body_no_dynamic))


def _measure_role(path: Path) -> dict:
    """对单个角色文件做可量化测量，返回测量字典。"""
    text = path.read_text(encoding="utf-8")
    fm, body, fm_chars = _parse_frontmatter(text)

    # 规范 §11 契约字段拆出单独计数，不计入 §4 软上限（见 _split_fm_contract）
    _fm_raw_match = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", text, re.DOTALL)
    if _fm_raw_match:
        _kept, _contract = _split_fm_contract(_fm_raw_match.group(1))
        fm_contract_chars = len(_fm_raw_match.group(1)) - len(_kept)
    else:
        fm_contract_chars = 0
    fm_governed_chars = fm_chars - fm_contract_chars

    dynamic_body = _last_dynamic_body(body)

    # 去掉 DYNAMIC 区域计算 body 长度。
    # ⚠️ 这是**可维护性**口径（含 §7/§8），仅作信息项报出，**不再用于判超限**。
    body_no_dynamic = _DYNAMIC_RE.sub(
        "<!-- DYNAMIC_START --><!-- DYNAMIC_END -->", body
    )
    body_no_dynamic_chars = len(body_no_dynamic)

    # ── prompt 口径（2026-08-16 修）─────────────────────────────────
    # 判超限只看**真正注入 system_prompt 的那段**：业务角色 §1-§6 /
    # 元角色全 body 减 DYNAMIC 与版本历史。直接复用引擎的抽取函数，不另写一份。
    # 抽取失败（业务角色缺章 / 乱序）时降级回旧口径并置 extract_ok=False ——
    # 审计器不能因单个角色结构不合规就崩掉整轮，那类问题由 T2.7 lint 单独报。
    _domain = str(fm.get("domain") or "").strip()
    try:
        prompt_text, prompt_path_used = _extract_role_prompt_sections(body, _domain)
        extract_ok = True
    except Exception as _e:
        prompt_text, prompt_path_used, extract_ok = body_no_dynamic, f"fallback:{_e.__class__.__name__}", False
    prompt_body_chars = len(prompt_text)

    section_chars = _section_char_counts(prompt_text)
    max_section = max(section_chars.values(), default=0)
    max_section_id = max(section_chars, key=section_chars.get, default="?") if section_chars else "?"

    # 检查 DYNAMIC 区域内各 patch 大小
    patches = _split_patches(dynamic_body)
    oversized_patches = [
        (i + 1, len(p))
        for i, p in enumerate(patches)
        if len(p) > LIMITS["single_patch"]
    ]

    # 检查 patch 标题格式
    patch_titles = _PATCH_HEADER_RE.findall(dynamic_body)
    patch_count = len(patch_titles)

    # 检查 DYNAMIC marker 是否被字面引用（含转义版）
    marker_literal_in_body = _check_dynamic_marker_literal(
        _DYNAMIC_RE.sub("", body)  # 去掉 DYNAMIC 区域后扫正文
    )

    # frontmatter 字段检查
    present = set(fm.keys())
    missing_required = REQUIRED_FIELDS - present
    present_forbidden = FORBIDDEN_FIELDS & present

    # list 字段类型校验（规范 §2.2）：null 与标量都判违规
    list_field_violations: list[str] = []
    for k in sorted(LIST_FIELDS & present):
        v = fm.get(k)
        if v is None:
            list_field_violations.append(f"`{k}` 是 null（应写 `[]`）")
        elif not isinstance(v, list):
            list_field_violations.append(
                f"`{k}` 不是 list（实为 {type(v).__name__}）"
            )

    # version 与 §8 版本历史一致性（规范 §3.4a）
    fm_version = str(fm.get("version", "")).strip()
    section8_max = _max_version_in_section8(body)
    version_mismatch = (
        bool(fm_version) and section8_max is not None and fm_version != section8_max
    )

    # P6：skill_refs 数量 + 引用 skill 文件的 trigger 完整性
    # skill_refs 列表本身长度（软上限 LIMITS["skill_refs_max"]）
    skill_refs_raw = fm.get("skill_refs") if isinstance(fm, dict) else None
    if isinstance(skill_refs_raw, list):
        skill_refs_paths = [str(x).strip() for x in skill_refs_raw if x]
    elif isinstance(skill_refs_raw, str):
        skill_refs_paths = [skill_refs_raw.strip()] if skill_refs_raw.strip() else []
    else:
        skill_refs_paths = []
    skill_refs_count = len(skill_refs_paths)
    skill_refs_over_limit = skill_refs_count > LIMITS["skill_refs_max"]

    # 引用 skill 文件的 trigger 完整性：缺 trigger.keywords / file_patterns / always
    # 的 skill 会被 skill_trigger.discover_role_skills fail-closed 跳过；本 lint 提前
    # 暴露，防止"角色声明 skill_refs 但触发器机制静默失效"。
    skill_trigger_gaps: list[str] = []
    for rel in skill_refs_paths:
        skill_path = VAULT_ROOT / rel
        if not skill_path.is_file():
            # 缺文件与 §10.7 load_role fallback 一致：不 fail_closed，仅记录
            skill_trigger_gaps.append(f"{rel}（文件缺失）")
            continue
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except OSError as e:
            skill_trigger_gaps.append(f"{rel}（读取失败：{e}）")
            continue
        skill_fm, _, _ = _parse_frontmatter(skill_text)
        if not _skill_trigger_valid(skill_fm):
            skill_trigger_gaps.append(f"{rel}（trigger 缺失或不完整）")

    # 章节序号检查
    found_sections = sorted(int(k) for k in section_chars if k.isdigit())

    # 是否有 DYNAMIC 标记
    has_dynamic_markers = "<!-- DYNAMIC_START -->" in text and "<!-- DYNAMIC_END -->" in text

    # 豁免判断
    is_meta = str(fm.get("domain", "")).strip() == "元"
    is_agent_generated = bool(fm.get("agent_generated", False))
    is_tiny = fm.get("role", "") in ("批判者", "用户体验者")

    # T2.7 白名单契约 lint
    # 业务角色（domain != 元）必须 §1-§6 完整 + §7 是运行时补丁 + §8 是版本历史
    # 元角色仅检查 DYNAMIC marker + 版本历史段存在
    # ⚠️ 直接扫原 body（不依赖 body_no_dynamic），避免 §6.4 DYNAMIC marker 滥用
    # 反模式（字面引用 marker 让 _DYNAMIC_RE 非贪婪匹配吞掉中间章节）干扰本 lint
    # 与 _split_sections 同样跳 fenced code block，否则内嵌产物模板里的
    # `## 1.` / `## 8.` 会污染章节表（见 _split_sections docstring）
    _TOP_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.+)$")
    top_sections: dict[int, str] = {}
    _in_fence = False
    for _line in body.split("\n"):
        _s = _line.lstrip()
        if _s.startswith("```") or _s.startswith("~~~"):
            _in_fence = not _in_fence
            continue
        if _in_fence:
            continue
        _m = _TOP_SECTION_RE.match(_line)
        if _m:
            top_sections[int(_m.group(1))] = _m.group(2).strip()

    prompt_whitelist_issues: list[str] = []
    if is_meta:
        # 元角色：DYNAMIC marker 已在 has_dynamic_markers 检查；只查版本历史存在
        has_version = any("版本历史" in title for title in top_sections.values())
        if not has_version:
            prompt_whitelist_issues.append("元角色缺『版本历史』章节")
    else:
        # 业务角色严格 §1-§6 完整
        missing_business_sections = [n for n in range(1, 7) if n not in top_sections]
        if missing_business_sections:
            prompt_whitelist_issues.append(
                f"业务角色 §1-§6 缺章：缺 §{missing_business_sections}"
            )
        # §7 应该是"运行时补丁"标题
        s7_title = top_sections.get(7, "")
        if s7_title and "运行时补丁" not in s7_title and "控制区" not in s7_title:
            prompt_whitelist_issues.append(
                f"业务角色 §7 标题应为『运行时补丁（控制区）』，实际：『{s7_title}』"
            )
        # §8 应该是"版本历史"
        s8_title = top_sections.get(8, "")
        if s8_title and "版本历史" not in s8_title:
            prompt_whitelist_issues.append(
                f"业务角色 §8 标题应为『版本历史』，实际：『{s8_title}』"
            )
        # lint_section_non_empty：§1-§6 每章有效正文须 ≥ min_section_chars。
        # 只对存在的章节判；缺章由上面的"缺章"检查负责，不重复报。
        eff = _section_effective_chars(body_no_dynamic)
        hollow = [
            (n, eff.get(str(n), 0))
            for n in range(1, 7)
            if n in top_sections and eff.get(str(n), 0) < LIMITS["min_section_chars"]
        ]
        if hollow:
            detail = "、".join(f"§{n}({c} chars)" for n, c in hollow)
            prompt_whitelist_issues.append(
                f"业务角色章节空壳：{detail} 剥离 HTML 注释与空白后不足 "
                f"{LIMITS['min_section_chars']} chars —— 标题存在但注入 system prompt 的实质内容近乎为零"
            )

    # 新业务角色更严格：agent_generated=true 不允许 prompt_whitelist 任何不合规
    prompt_whitelist_level = "OK"
    if prompt_whitelist_issues:
        if not is_meta and is_agent_generated:
            prompt_whitelist_level = "ERROR_NEW"
        elif not is_meta:
            prompt_whitelist_level = "WARN_NORMALIZE"
        else:
            prompt_whitelist_level = "WARN_META"
    # 空壳章节无条件升 ERROR：一个无使命 / 无职责 / 无边界的 agent 被调度，
    # 危害与"缺章"等同，不因角色是人写的（agent_generated=false）而降级。
    if not is_meta and any("章节空壳" in i for i in prompt_whitelist_issues):
        prompt_whitelist_level = "ERROR_HOLLOW"

    return {
        "filename": path.name,
        "role": fm.get("role", path.stem),
        "domain": fm.get("domain", ""),
        "version": fm.get("version", ""),
        "agent_generated": is_agent_generated,
        "is_meta": is_meta,
        "is_tiny": is_tiny,
        # lengths
        "frontmatter_chars": fm_governed_chars,
        # 信息项，不设阈值（阈值无实测依据）。契约膨胀仍可见，但不再误报为 FM 超限。
        "frontmatter_contract_chars": fm_contract_chars,
        "frontmatter_chars_total": fm_chars,
        # 可维护性口径（含 §7/§8）：信息项，**不判超限**（2026-08-16 起）
        "body_no_dynamic_chars": body_no_dynamic_chars,
        # prompt 口径：真正注入 system_prompt 的字符数，判超限只看这个
        "prompt_body_chars": prompt_body_chars,
        "prompt_path_used": prompt_path_used,
        "prompt_extract_ok": extract_ok,
        "dynamic_chars": len(dynamic_body),
        "max_section_chars": max_section,
        "max_section_id": max_section_id,
        "section_chars": section_chars,
        # field checks
        "missing_required": sorted(missing_required),
        "present_forbidden": sorted(present_forbidden),
        "list_field_violations": list_field_violations,
        "version_mismatch": version_mismatch,
        "section8_max_version": section8_max,
        # structure
        "found_sections": found_sections,
        "has_dynamic_markers": has_dynamic_markers,
        # DYNAMIC content
        "patch_count": patch_count,
        "oversized_patches": oversized_patches,
        "marker_literal_in_body": marker_literal_in_body,
        # limits exceeded
        "fm_over_limit": fm_governed_chars > LIMITS["frontmatter"],
        "body_over_limit": prompt_body_chars > LIMITS["prompt_body"],
        "section_over_limit": max_section > LIMITS["single_section"],
        "dynamic_over_limit": len(dynamic_body) > LIMITS["dynamic"],
        # T2.7 prompt 白名单契约 lint
        "prompt_whitelist_issues": prompt_whitelist_issues,
        "prompt_whitelist_level": prompt_whitelist_level,
        # P6：skill_refs 治理 lint
        "skill_refs_count": skill_refs_count,
        "skill_refs_over_limit": skill_refs_over_limit,
        "skill_trigger_gaps": skill_trigger_gaps,
    }


def _format_measurements(measures: list[dict]) -> str:
    """把测量结果格式化为人类可读的 markdown 表格，注入 user prompt。"""
    lines = [
        "# 程序层测量结果（字符长度 / 字段合规 / DYNAMIC 合规）",
        "",
        "> **正文列口径（2026-08-16 修）**：`进prompt` 是真正注入 system_prompt 的字符数"
        "（业务角色 §1-§6 / 元角色全 body 减 DYNAMIC 与版本历史），**判超限只看它**；"
        "`全文` 含 §7/§8，仅作可维护性信息项。旧版把 §8 版本历史算进超限判定，"
        "导致 4 假阳性 / 0 真阳性，并反向惩罚写版本历史。",
        "",
        "| 角色 | domain | FM字符 | 正文(进prompt/全文) | 最大章节字符(§) | DYNAMIC字符 | 超限 | 缺必填 | 有禁止字段 | DYNAMIC对 | 补丁数 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in measures:
        over = []
        if m["fm_over_limit"]:
            over.append("FM")
        if m["body_over_limit"]:
            over.append("正文")
        if m["section_over_limit"]:
            over.append(f"§{m['max_section_id']}")
        if m["dynamic_over_limit"]:
            over.append("DYNAMIC")
        if m["oversized_patches"]:
            over.append(f"P{m['oversized_patches']}")

        # 契约字符只在非零时附注，避免给 24 个无契约角色的表格加噪音
        _contract = m.get("frontmatter_contract_chars") or 0
        fm_cell = f"{m['frontmatter_chars']}" + (f" (+{_contract}契约)" if _contract else "")

        lines.append(
            f"| {m['role']} | {m['domain']} "
            f"| {fm_cell} "
            f"| {m['prompt_body_chars']}/{m['body_no_dynamic_chars']}"
            f"{'' if m.get('prompt_extract_ok', True) else ' ⚠️降级'} "
            f"| {m['max_section_chars']}(§{m['max_section_id']}) "
            f"| {m['dynamic_chars']} "
            f"| {' '.join(over) or '—'} "
            f"| {', '.join(m['missing_required']) or '—'} "
            f"| {', '.join(m['present_forbidden']) or '—'} "
            f"| {'✓' if m['has_dynamic_markers'] else '✗'} "
            f"| {m['patch_count']} |"
        )

    lines.append("")
    lines.append("## 详细异常")
    for m in measures:
        issues: list[str] = []
        if m["missing_required"]:
            issues.append(f"缺必填字段：{m['missing_required']}")
        if m["present_forbidden"]:
            issues.append(f"含禁止字段：{m['present_forbidden']}")
        for v in m.get("list_field_violations", []):
            issues.append(f"规范 §2.2 list 字段类型违规：{v}")
        if m.get("version_mismatch"):
            issues.append(
                f"规范 §3.4a 版本漂移：frontmatter `version: {m['version']}` "
                f"≠ §8 版本历史最大版本号 {m['section8_max_version']}"
            )
        if not m.get("prompt_extract_ok", True):
            issues.append(
                f"prompt 抽取失败（`{m.get('prompt_path_used')}`）→ 正文口径已降级为"
                f"「全文减 DYNAMIC」，本行超限判定偏严不可信；根因见下方 T2.7 lint 段"
            )
        if not m["has_dynamic_markers"]:
            issues.append("缺 DYNAMIC 标记对（<!-- DYNAMIC_START/END -->）")
        if m["marker_literal_in_body"]:
            issues.append("正文字面引用了 DYNAMIC_START marker（破坏 regex）")
        if m["oversized_patches"]:
            for idx, size in m["oversized_patches"]:
                issues.append(f"DYNAMIC 第 {idx} 条 patch 超限：{size} > {LIMITS['single_patch']} chars")
        # P6: skill_refs 治理
        if m.get("skill_refs_over_limit"):
            issues.append(
                f"skill_refs 数量 {m['skill_refs_count']} > 软上限 "
                f"{LIMITS['skill_refs_max']} → 建议 [SHRINK?]（收敛到 `_通用/` 或拆角色）"
            )
        for gap in m.get("skill_trigger_gaps", []):
            issues.append(f"skill_refs 引用的 skill trigger 缺失：{gap}")
        if issues:
            lines.append(f"\n### {m['role']}（{m['filename']}）")
            for iss in issues:
                lines.append(f"- {iss}")

    # T2.7 白名单契约 lint 段落
    lines.append("")
    lines.append("## T2.7 prompt 白名单契约 lint")
    lines.append("")
    lines.append("| 角色 | domain | 等级 | 问题 |")
    lines.append("|---|---|---|---|")
    for m in measures:
        level = m.get("prompt_whitelist_level", "OK")
        issues = m.get("prompt_whitelist_issues", [])
        if level == "OK":
            continue
        badge = {
            "ERROR_NEW": "🔴 ERROR",
            "ERROR_HOLLOW": "🔴 ERROR",
            "WARN_NORMALIZE": "🟡 WARN",
            "WARN_META": "🔵 INFO",
        }.get(level, level)
        lines.append(
            f"| {m['role']} | {m['domain']} | {badge} | {'; '.join(issues)} |"
        )
    return "\n".join(lines)


def _scan_vault_stem_uniqueness() -> dict[str, list[Path]]:
    """扫 vault 全 .md 文件，按 stem 分组返回重名项。

    与 engine.wikilink.resolve_target 的索引逻辑对齐：排除 _STEM_SCAN_EXCLUDES
    下的文件。命名规则要求"角色 / 工作流 / 规则 / skill / 项目记录等命名空间
    stem 全 vault 唯一"，违反时 wikilink 解析会抛 DuplicateStemError。
    本扫描在审计阶段提前暴露这类冲突，避免运行时崩溃。

    返回 dict: stem → [path1, path2, ...]，只包含重名（len >= 2）的 stem。
    无重名 → 空 dict。
    """
    from collections import defaultdict
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in VAULT_ROOT.rglob("*.md"):
        try:
            rel = p.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            continue
        if any(rel.startswith(prefix) for prefix in _STEM_SCAN_EXCLUDES):
            continue
        if _is_domain_rule_adapter(rel):
            continue
        groups[p.stem].append(p)
    return {stem: sorted(paths) for stem, paths in groups.items() if len(paths) >= 2}


def _format_stem_uniqueness(dupes: dict[str, list[Path]]) -> str:
    """把 stem 重名清单格式化为 markdown，注入到审计报告。"""
    excludes = "、".join(_STEM_SCAN_EXCLUDES) + "、00-系统/规则/<域>/（跨域适配器）"
    if not dupes:
        return (
            "# Vault stem 唯一性扫描\n\n"
            f"✅ 未发现 stem 重名（已排除：{excludes}）"
        )
    lines = [
        "# Vault stem 唯一性扫描",
        "",
        f"⚠️ 发现 {len(dupes)} 组 stem 重名（违反 vault命名规则.md，"
        f"wikilink 命中会抛 DuplicateStemError）",
        "",
    ]
    for stem in sorted(dupes):
        paths = dupes[stem]
        lines.append(f"## `{stem}.md`（{len(paths)} 处）")
        for p in paths:
            try:
                rel = p.relative_to(VAULT_ROOT).as_posix()
            except ValueError:
                rel = str(p)
            lines.append(f"- `{rel}`")
        lines.append("")
    lines.append(
        "**修复方向**：重命名为唯一 stem，或在 wikilink 处用完整路径消歧"
        "（如 `[[20-知识/角色技能/架构师/A1-代码量预算分账]]`）。"
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="角色审计器：审计指定 / 全部角色基因文件")
    p.add_argument("--dry-run", action="store_true", help="只打印测量结果，不调 LLM、不写盘")
    p.add_argument(
        "--target", action="append", default=None,
        help="治理对象（可重复 / 逗号分隔 / 'all'）；缺省审计全部角色",
    )
    p.add_argument(
        "--audit-outputs", action="store_true",
        help="切换到产物审计模式（扫 10-项目/*/PRD.md 越界 pattern），不跑角色基因审计",
    )
    return p.parse_args()


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def main() -> int:
    args = _parse_args()

    # 产物审计模式：与角色基因审计正交，独立路径不动 ROLE 状态机
    # （产物审计是治理 vault 产物的产物，不影响角色审计器自己的 busy/idle）
    if getattr(args, "audit_outputs", False):
        return _run_pm_output_audit(dry_run=bool(args.dry_run))

    dry_run = bool(args.dry_run)
    targets = parse_targets(args.target)   # None = 全部
    date_stamp = _today_stamp()

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 1) 收集角色文件（rglob：支持 music/ 等域子目录；2026-05-24 D-9 全子目录方案）
    rgd = role_genes_dir()
    all_role_files = sorted(rgd.rglob("角色-*.md"))
    role_files = [
        f for f in all_role_files
        if f.name != SELF_FILENAME
        and (targets is None or any(t in f.stem for t in targets))
    ]

    if not role_files:
        print(f"[{ROLE}] 没有找到可审计的角色文件。", file=sys.stderr)
        set_role_status(ROLE, status="failed", enforce_transition=False)
        return 2

    print(f"[{ROLE}] 审计 {len(role_files)} 个角色：{[f.name for f in role_files]}")

    # 2) 程序层测量
    measures = [_measure_role(f) for f in role_files]
    measurement_table = _format_measurements(measures)

    # 2b) Vault stem 唯一性扫描（与角色审计正交：扫整个 vault，不受 --target 限制）
    stem_dupes = _scan_vault_stem_uniqueness()
    stem_table = _format_stem_uniqueness(stem_dupes)

    print(measurement_table)
    print()
    print(stem_table)

    if dry_run:
        print(f"[{ROLE}] --dry-run 模式，未调用 LLM、未写盘。")
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": "*",
            "result": "dry_run", "roles_checked": len(role_files),
        })
        return 0

    # 3) 规范文档 + 所有角色全文
    spec_path = VAULT_ROOT / SPEC_REL
    inputs = [spec_path] + role_files
    context = read_input_files(inputs)

    # 4) system prompt（角色审计器基因）
    system_prompt = build_system_prompt(ROLE, project=None)

    # 5) 报告路径
    audit_dir = VAULT_ROOT / AUDIT_DIR_REL
    audit_dir.mkdir(parents=True, exist_ok=True)
    report_rel = f"{AUDIT_DIR_REL}/角色基因审计-{date_stamp}.md"

    # 6) user prompt
    role_list = "\n".join(f"  - {m['role']}（{m['filename']}）" for m in measures)
    user_prompt = (
        f"# 审计任务\n\n"
        f"对照 `{SPEC_REL}` 审计以下 {len(role_files)} 个角色基因文件：\n{role_list}\n\n"
        f"# 程序层预计算（已完成，供你参考）\n\n{measurement_table}\n\n"
        f"---\n\n"
        f"{stem_table}\n\n"
        f"---\n\n"
        f"# 输入文件全文（规范 + 各角色）\n\n{context}\n\n"
        f"---\n\n"
        f"# 你的任务\n\n"
        f"按角色基因第 3-5 节，对每个角色做语义层审计（程序层测量已完成，你只需做语义判断）：\n\n"
        f"1. **字段重复**（规范 §6.1）：frontmatter 值是否在正文中重复描述\n"
        f"2. **禁止事项过散**（规范 §6.2）：§4 边界规则是否散落在正文其他节\n"
        f"3. **全局规则在角色内**（规范 §6.3）：技术栈 / 架构规则等是否直接写在角色而非引用\n"
        f"4. **DYNAMIC 区滥用**（规范 §6.4）：已 GRADUATE 补丁是否仍残留；DYNAMIC 是否长期堆积\n"
        f"5. **模糊禁止**（规范 §6.5）：§4 边界规则是否缺乏可 grep 的硬约束\n"
        f"6. **角色名不一致**（规范 §6.6）：引用其他角色时是否混用别名 / 中文名\n"
        f"7. **越界改他角色定义**（规范 §6.7）：是否在角色 X 里定义修改角色 Y 的逻辑\n"
        f"8. **技能未外迁**（规范 §6.8）：§6 / 单 patch 超规范上限且含可独立 grep gate / 反例 / 代码块 / 跨角色复用规则\n"
        f"9. **豁免识别**：元角色（domain=元）/ 极小角色 / 新生角色（agent_generated=true）按规范 §7 豁免条件先检查\n\n"
        f"每个偏离项必须引用规范具体条款（如'规范 §6.1'）+ 建议修复方向。\n\n"
        f"严重度分级：\n"
        f"- **严重**：缺必填字段 / 无 DYNAMIC 标记对 / marker 被字面引用（破坏 regex）\n"
        f"- **警告**：超长 [SHRINK?] / 单 patch 超长 / 禁止事项过散 / 全局规则在角色内 / 模糊禁止\n"
        f"- **建议**：字段重复描述 / 角色名不一致 / 越界提及\n\n"
        f"---\n\n"
        f"# [SPLIT?] 建议（强制：超限角色必须给出结构化外迁建议）\n\n"
        f"对每个 §6 超 1500 chars / 单 patch 超 1200 chars 的角色，按规范 §10「Skill 引用机制」**逐条**列出可外迁段落，每条 `[SPLIT?]` 必须包含 4 个字段：\n\n"
        f"1. **source**：源段落定位（如 `角色-X.md §6 步骤 3 子项 (1)` 或 `DYNAMIC patch [YYYY-MM-DD][KEEP] Xn`）\n"
        f"2. **size**：估算字符数（让用户判断收益）\n"
        f"3. **target**：建议 skill 文件路径（如 `20-知识/角色技能/{{角色}}/{{patch_id}}-{{标题}}.md`，跨角色共享用 `_通用/`）\n"
        f"4. **rationale**：为什么这段值得外迁（含可独立 grep gate / 反例 / 跨角色复用 等）\n\n"
        f"已 split 的角色（frontmatter 含 skill_refs）：若仍超限，新建议必须不与现有 skill_refs 重复\n"
        f"未超限的角色：不要发明 [SPLIT?] 建议\n"
        f"已被规范 §7 豁免的元角色 / 极小角色：豁免内不下 [SPLIT?]\n\n"
        f"---\n\n"
        f"# 输出（强制格式）\n\n"
        f"**你必须且只能输出一个 FILE 块**，内容是完整审计报告。FILE 块外不能有任何其他文字。\n\n"
        f"<!-- FILE: {report_rel} -->\n"
        f"---\n"
        f"type: audit\n"
        f"created: {utc_now()}\n"
        f"roles_audited: {len(role_files)}\n"
        f"---\n\n"
        f"# 角色基因审计报告 - {date_stamp}\n\n"
        f"## 0. 健康评分\n"
        f"（写实际数字）\n"
        f"- 完全合规角色：X / {len(role_files)}\n"
        f"- 严重问题：X 项\n"
        f"- 警告：X 项（[SHRINK?]：X 个）\n"
        f"- 建议：X 项\n\n"
        f"## 1. 分角色审计\n"
        f"（每个角色一节，引用上表的实际测量数字）\n\n"
        f"### 角色：<role>（<filename>）\n"
        f"**程序层测量**：FM N字符 / 正文 N字符 / DYNAMIC N字符\n"
        f"**偏离项**：\n"
        f"- [严重|警告|建议] 规范 §X.X：<偏离描述> → 建议：<修复方向>\n"
        f"若无偏离：合规\n\n"
        f"## 2. [SPLIT?] 外迁建议（结构化）\n"
        f"（只对超限角色出条目；未超限的不要凑数）\n\n"
        f"### 角色：<role>\n"
        f"- **[SPLIT?]** source: <段落定位> | size: ~N chars | target: `20-知识/角色技能/<角色>/<id>-<标题>.md` | rationale: <一句话>\n"
        f"- ...\n\n"
        f"## 3. 整体建议\n"
        f"（跨角色共性问题 + 优先处理顺序；包括规范文档本身是否需要更新）\n"
        f"<!-- /FILE -->\n"
    )

    # 7) 调用 LLM
    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] Claude API 调用失败：{e}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE,
            "result": "failed", "error": str(e),
        })
        return 1

    # 8) 写盘
    output_files = parse_claude_output_to_files(raw_output)
    written: list[str] = []

    if not output_files:
        dest = audit_dir / f"角色基因审计-{date_stamp}.md"
        write_output_atomic(dest, raw_output)
        written.append(str(dest))
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        for rel_path, content in output_files.items():
            # 安全防线：只允许写入审计报告，不允许修改角色文件
            if "角色基因" in rel_path and "审计" not in rel_path:
                print(f"[{ROLE}] ⚠️  拒绝写入角色文件 {rel_path}（审计者只读）", file=sys.stderr)
                continue
            dest = resolve_path(rel_path, project=None)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_path)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE,
        "result": "success", "outputs": written,
        "roles_audited": len(role_files),
    })
    print(f"[{ROLE}] 完成。报告：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
