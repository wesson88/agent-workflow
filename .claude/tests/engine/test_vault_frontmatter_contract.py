"""
test_vault_frontmatter_contract.py — vault frontmatter 规范契约 lint

守的是 vault [[frontmatter规范]] + [[frontmatter-类型]] 两份规则文档，
范式参照 test_se_contract_lint.py（module fixture + class Test* + broken 累积断言）。

**为什么放这里而不是 git hook**：
2026-08-16 评估过「入库前 git 闸」并放弃，一条硬理由是 `10-项目/`（210 篇）与
`98-待办/` 整目录 gitignored —— git 闸天然覆盖不到 33% 的笔记，而 2026-08-16 修的
C 类链接错误恰恰全在 `10-项目/music/西关十字/`。pytest 扫的是**文件系统**不是 git，
没有这个盲区。用户手动跑测试时自然触发。

覆盖：
- 类型文档自身健康：表格可解析 / canonical 命名约定 / 别名 target 有效 / 两表无交集
- type 值在册：入库范围所有 type ∈ canonical ∪ 别名（防新造野值）
- type 形态：值必须是字符串；已知的 list 型存量不得扩散
- 格式硬约束：复用 engine.frontmatter_links.check_frontmatter 扫**全 vault**（含 gitignored 目录）
- 高合规类别必填字段（实测 100% 的三类，防倒退）
- 规范文档互指不断链（vault 目录重组未同步的事故本库踩过，见 test_se_contract_lint.TestVaultPathSync）
"""

from __future__ import annotations

import re

import pytest
import yaml

from engine.config import VAULT_ROOT
from engine.frontmatter_links import check_frontmatter

SPEC_REL = "00-系统/规则/frontmatter规范.md"
TYPES_REL = "00-系统/规则/frontmatter-类型.md"

# 入库范围 —— 与 [[frontmatter-类型]] §1 适用边界一致。
# `10-项目/` 的产物 type 由产物注册表 + music 产物schema 管辖，不在本表管辖内。
IN_REPO_PREFIXES = ("00-系统/", "20-知识/", "80-收件箱/", "98-待办/")

# 格式硬约束的豁免清单。**只允许放已挂待办、有明确处置结论的存量**，
# 每条必须写清为什么不在本轮修 —— 清单变长就是治理失控的信号。
FORMAT_EXEMPT = {
    # 双 frontmatter 块。两块键名零重叠、合并无冲突，但属历史产物的内容变更，
    # 需用户拍板。已挂 98-待办 P2（2026-08-16 创建）。
    "10-项目/music/纸飞机/Suno-prompt.md",
    # `source: 综合 [[a]] + [[b]] v0.0–v0.3` 是散文句不是链接列表。
    # 98-待办 2026-08-15 条目明示「不属此类不要动」。
    "10-项目/pain-radar/PRD.md",
}

# `type` 写成 list 的存量（skillmind 导入把 doc_type 语义写进了 type）。
# 依据：2026-08-16 全量实测 15 篇，全部落在 20-知识/角色技能/se/。
# 断言口径是「不扩散」而不是「清零」—— 归并挂 98-待办，归并后本常量降到 0。
LIST_TYPE_DIR = "20-知识/角色技能/se/"
LIST_TYPE_BASELINE = 15

# 实测覆盖率 100% 的类别 → 追认为必填，本 lint 防倒退。
# 依据：2026-08-16 全量实测，三类均零缺失（见 [[frontmatter规范]] §4 矩阵）。
STRICT_REQUIRED = {
    "00-系统/工作流模板": ["type", "name", "description", "domain", "halt_on_failure", "steps"],
    "00-系统/仪表盘": ["type", "title", "created"],
    "00-系统/产物注册表": ["artifact", "domain", "path_template", "format", "producer", "consumers"],
}

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def _split_frontmatter_text(text: str) -> str | None:
    """取开头 frontmatter 块的原始文本（不解析）。无则 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _iter_notes():
    """遍历 vault 全部 .md（**含 gitignored 目录**），跳过备份、草稿与 Obsidian 内部目录。

    - `.backup*` 跳过依据：98-待办 2026-08-15「备份保持原样」决定
    - `99-临时/` 跳过依据：[[vault命名规则]] §2.9 明列为排除区（草稿，不参与命名空间），
      草稿不该被规范卡住
    """
    for path in sorted(VAULT_ROOT.rglob("*.md")):
        parts = path.parts
        if any(x in parts for x in (".obsidian", ".git", ".trash", "Excalidraw", "99-临时")):
            continue
        if any(x.startswith(".backup") for x in parts):
            continue
        yield path, path.relative_to(VAULT_ROOT).as_posix()


# ── 检查逻辑（纯函数，便于用合成数据反向验证 lint 真的会红）──────────
# Note 三元组：(相对路径, 全文, frontmatter dict 或 None)

def _collect_type_violations(notes, canonical, aliases) -> list[str]:
    known = canonical | set(aliases)
    broken = []
    for rel, _text, fm in notes:
        if fm is None or "type" not in fm:
            continue
        if not rel.startswith(IN_REPO_PREFIXES):
            continue
        value = fm["type"]
        if isinstance(value, list):
            continue  # list 形态由 _collect_list_type_offenders 单独管
        if str(value) not in known:
            broken.append(f"  {rel}\n      type: {value!r} 未登记")
    return broken


def _collect_format_violations(notes, exempt=FORMAT_EXEMPT) -> list[str]:
    broken = []
    for rel, text, _fm in notes:
        if rel in exempt:
            continue
        for problem in check_frontmatter(text):
            broken.append(f"  {rel}\n      {problem}")
    return broken


def _collect_list_type_offenders(notes) -> list[str]:
    return [
        rel for rel, _text, fm in notes
        if fm is not None and isinstance(fm.get("type"), list)
    ]


def _collect_required_violations(notes) -> list[str]:
    broken = []
    for rel, _text, fm in notes:
        for prefix, required in STRICT_REQUIRED.items():
            if not rel.startswith(prefix + "/"):
                continue
            if rel.rsplit("/", 1)[-1].startswith("_"):
                continue  # `_config.md` 等元条目有自己的 schema
            if fm is None:
                broken.append(f"  {rel}\n      无 frontmatter 或 YAML 无法解析")
                continue
            missing = [k for k in required if k not in fm]
            if missing:
                broken.append(f"  {rel}\n      缺必填字段 {missing}")
    return broken


def _section(doc: str, start: str, end: str) -> str:
    """截取 markdown 文档的 `## start` 到 `## end` 之间的正文。"""
    i = doc.find(start)
    j = doc.find(end)
    assert i != -1, f"类型文档缺章节 {start}"
    assert j > i, f"类型文档章节顺序异常：{start} 应在 {end} 之前"
    return doc[i:j]


def _parse_type_doc() -> tuple[set[str], dict[str, str]]:
    """从 [[frontmatter-类型]] 正文表格解析 (canonical 集合, 别名→canonical 映射)。

    正文表格是唯一来源（frontmatter 只放元信息），所以这里解析表格而不是 YAML。
    解析失败即测试失败 —— 表格格式被破坏本身就该被拦住。
    """
    doc = (VAULT_ROOT / TYPES_REL).read_text(encoding="utf-8")

    canonical: set[str] = set()
    for line in _section(doc, "## §3", "## §4").splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        found = _BACKTICK_RE.findall(cells[1])
        if found:
            canonical.add(found[0])

    aliases: dict[str, str] = {}
    for line in _section(doc, "## §4", "## §5").splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 4:
            continue
        old = _BACKTICK_RE.findall(cells[1])
        new = _BACKTICK_RE.findall(cells[2])
        if old and new:
            aliases[old[0]] = new[0]

    return canonical, aliases


@pytest.fixture(scope="module")
def type_registry() -> tuple[set[str], dict[str, str]]:
    return _parse_type_doc()


@pytest.fixture(scope="module")
def notes() -> list[tuple[str, str, dict | None]]:
    """(相对路径, 全文, frontmatter dict 或 None)。YAML 挂 → dict 为 None。"""
    out = []
    for path, rel in _iter_notes():
        text = path.read_text(encoding="utf-8")
        fm_text = _split_frontmatter_text(text)
        fm: dict | None = None
        if fm_text is not None:
            try:
                parsed = yaml.safe_load(fm_text)
                fm = parsed if isinstance(parsed, dict) else None
            except yaml.YAMLError:
                fm = None
        out.append((rel, text, fm))
    return out


class TestNotVacuous:
    """防空转：扫描或解析退化成空集时，上面所有断言都会**真空通过**。

    这一组守的就是这个 —— 一套永远绿的 lint 比没有 lint 更糟，
    它制造「已经管住了」的错觉（[[产物schema]] `produced_by` 0/133 就是先例）。

    下限不是质量阈值，是**结构性故障探测线**：
    依据 = 2026-08-16 实测（637 篇 / 10-项目 210 篇 / 27 canonical / 15 别名），
    各留约 20-30% 余量，只在"扫到 0 或个位数"这种路径级故障时才触发。
    """

    def test_scan_reaches_vault(self, notes):
        assert len(notes) >= 500, (
            f"只扫到 {len(notes)} 篇（2026-08-16 实测 637）——"
            f"VAULT_ROOT 或 _iter_notes 的排除规则可能出问题了"
        )

    def test_scan_covers_gitignored_dirs(self, notes):
        """本测试存在的核心理由：覆盖 git 闸够不到的 33%。

        `10-项目/` 整目录 gitignored，2026-08-16 修的 C 类链接错误全在那里面。
        这条断言一旦失败，说明这套 lint 退化成了 git hook 的等价物。
        """
        in_project = [rel for rel, _t, _f in notes if rel.startswith("10-项目/")]
        assert len(in_project) >= 100, (
            f"只扫到 {len(in_project)} 篇 10-项目/ 笔记（2026-08-16 实测 210）——"
            f"gitignored 目录未被覆盖，本 lint 失去相对 git 闸的核心优势"
        )


class TestTypeDocHealth:
    """类型文档自身必须自洽 —— 它是 type 的唯一来源，它错了下游全错。"""

    def test_tables_parse(self, type_registry):
        canonical, aliases = type_registry
        assert len(canonical) >= 20, (
            f"§3 类型表只解析出 {len(canonical)} 个 canonical（2026-08-16 实测 27）——"
            f"表格格式可能被破坏，导致 type 校验部分失效"
        )
        assert len(aliases) >= 10, (
            f"§4 别名表只解析出 {len(aliases)} 条（2026-08-16 实测 15）——"
            f"解析退化会让存量别名被误报成野值"
        )

    def test_canonical_naming_convention(self, type_registry):
        """[[frontmatter-类型]] §5：多词值 kebab-case，不用下划线 / 中文 / 大写。"""
        canonical, _ = type_registry
        broken = [
            f"  `{v}` 不符合 kebab-case（应为小写字母/数字/连字符）"
            for v in sorted(canonical)
            if not re.fullmatch(r"[a-z][a-z0-9-]*", v)
        ]
        assert not broken, (
            "§3 canonical 值违反 §5 命名约定：\n" + "\n".join(broken)
        )

    def test_alias_targets_are_canonical(self, type_registry):
        """别名必须指向真实存在的 canonical，否则归并时会指向空。"""
        canonical, aliases = type_registry
        broken = [
            f"  `{old}` → `{new}`，但 `{new}` 不在 §3 类型表里"
            for old, new in sorted(aliases.items())
            if new not in canonical
        ]
        assert not broken, "§4 别名指向了不存在的 canonical：\n" + "\n".join(broken)

    def test_no_overlap(self, type_registry):
        """同一个值不能既是 canonical 又是别名 —— 那样归并方向就自相矛盾。"""
        canonical, aliases = type_registry
        overlap = sorted(canonical & set(aliases))
        assert not overlap, f"这些值同时出现在 §3 与 §4：{overlap}"


class TestTypeValues:
    """vault 实际用的 type 值必须在册。防的是「又造一个新写法」。"""

    def test_all_in_repo_types_registered(self, notes, type_registry):
        canonical, aliases = type_registry
        broken = _collect_type_violations(notes, canonical, aliases)
        assert not broken, (
            f"以下 type 值不在 {TYPES_REL} 的 §3 类型表或 §4 别名表里。\n"
            f"要么改用已登记的值，要么按 §6 流程往类型表加一行：\n"
            + "\n".join(broken)
        )


class TestTypeShape:
    """type 必须是字符串。多维分类另起字段（[[frontmatter-类型]] §5）。"""

    def test_list_typed_notes_do_not_spread(self, notes):
        offenders = _collect_list_type_offenders(notes)
        outside = sorted(r for r in offenders if not r.startswith(LIST_TYPE_DIR))
        assert not outside, (
            f"`type` 写成 list 的存量只应存在于 {LIST_TYPE_DIR}（skillmind 导入），"
            f"以下文件在此之外：\n" + "\n".join(f"  {r}" for r in outside)
        )
        assert len(offenders) <= LIST_TYPE_BASELINE, (
            f"`type` 是 list 的文件从 {LIST_TYPE_BASELINE} 篇涨到 {len(offenders)} 篇。"
            f"新笔记不要把多维分类写进 type —— 另起字段。"
        )


class TestFormatHardConstraints:
    """[[frontmatter规范]] §2 格式硬约束，扫**全 vault**含 gitignored 目录。

    复用 engine.frontmatter_links.check_frontmatter，不重复实现一份检查逻辑。
    """

    def test_no_new_format_violations(self, notes):
        broken = _collect_format_violations(notes)
        assert not broken, (
            "frontmatter 格式违规（见 [[frontmatter规范]] §2）。\n"
            "链接写法类问题在 runner 落盘时会被 normalize_frontmatter_links 自动修，\n"
            "出现在这里说明是人工写的笔记或非 runner 路径产出：\n"
            + "\n".join(broken)
        )

    def test_exempt_list_stays_minimal(self, notes):
        """豁免清单里的文件必须真的还存在 —— 修好了就该从清单里删掉。"""
        existing = {rel for rel, _t, _f in notes}
        stale = sorted(FORMAT_EXEMPT - existing)
        assert not stale, (
            "FORMAT_EXEMPT 里的文件已不存在，请删除对应豁免条目：\n"
            + "\n".join(f"  {s}" for s in stale)
        )


class TestStrictRequiredFields:
    """实测 100% 覆盖的类别 → 追认必填，本 lint 防倒退（[[frontmatter规范]] §4）。"""

    def test_high_compliance_classes_keep_required_fields(self, notes):
        broken = _collect_required_violations(notes)
        assert not broken, (
            "以下类别实测曾 100% 合规，现在出现缺失（倒退）：\n" + "\n".join(broken)
        )


class TestSpecDocsWiring:
    """规范文档之间的互指不能断链。

    本库踩过这类事故：vault 目录重组后代码里的规则路径读空 8 周无告警
    （见 test_se_contract_lint.TestVaultPathSync）。规则文档之间的引用同理。
    """

    def test_spec_and_type_docs_exist(self):
        for rel in (SPEC_REL, TYPES_REL):
            assert (VAULT_ROOT / rel).is_file(), f"规范文档缺失：{rel}"

    def test_spec_points_to_type_doc(self):
        spec = (VAULT_ROOT / SPEC_REL).read_text(encoding="utf-8")
        assert "[[frontmatter-类型]]" in spec, (
            "frontmatter规范 必须指向类型文档而不是自己内嵌枚举（解耦要求）"
        )

    def test_spec_does_not_inline_type_enum(self):
        """规范里不许复制枚举 —— 复制了就会和类型文档漂移。"""
        canonical, _ = _parse_type_doc()
        spec = (VAULT_ROOT / SPEC_REL).read_text(encoding="utf-8")
        # 规范正文里出现的 canonical 值（排除它自己 frontmatter 用的 `rule`）
        inlined = sorted(
            v for v in canonical
            if v != "rule" and f"`{v}`" in spec
        )
        assert len(inlined) <= 2, (
            f"frontmatter规范 正文里出现了 {len(inlined)} 个类型值：{inlined}。\n"
            f"枚举应只存在于 {TYPES_REL}，规范里最多举一两个例子。"
        )

    def test_referenced_rule_docs_exist(self):
        """§1.2 指向的 4 份 canonical 文档必须真实存在。"""
        referenced = [
            "00-系统/规则/角色基因规范.md",
            "00-系统/规则/产物注册表规范.md",
            "00-系统/规则/music/产物schema.md",
            "00-系统/规则/capability注册表规范.md",
        ]
        missing = [r for r in referenced if not (VAULT_ROOT / r).is_file()]
        assert not missing, (
            "frontmatter规范 §1.2 指向的规则文档不存在（vault 重组未同步？）：\n"
            + "\n".join(f"  {m}" for m in missing)
        )

    def test_artifact_schema_does_not_duplicate_link_rules(self):
        """music 产物schema 只应指向 §2，不再自带一份链接写法对照表。"""
        text = (VAULT_ROOT / "00-系统/规则/music/产物schema.md").read_text(encoding="utf-8")
        assert "[[frontmatter规范]]" in text, "产物schema 应指向 frontmatter规范 §2"
        assert "**A 类**" not in text, (
            "产物schema 又出现了 A/B/C 三类对照表 —— 那是 frontmatter规范 §2 的内容，"
            "两处维护迟早漂移"
        )


class TestLintsActuallyFire:
    """反向验证：给合成的违规数据，上面每条 lint 都必须真的报出来。

    **为什么必须有这一组**：本库最典型的失败模式就是「声明了但没实施」——
    [[产物schema]] `## 通用规则` 要求 `produced_by`，133 份产物遵守 0 次；
    music 8 角色声明了 rule_refs 但 4 天没有 skill 消费。
    一条永远不会红的 lint 属于同一类故障，且比没有 lint 更糟（它制造"已经管住了"的错觉）。

    用合成数据而不是往 vault 里塞违规文件：不污染 vault，也不依赖清理逻辑。
    """

    def test_unregistered_type_is_caught(self):
        notes = [("20-知识/项目记录/x.md", "", {"type": "野生类型"})]
        assert _collect_type_violations(notes, {"project-record"}, {})

    def test_registered_type_passes(self):
        notes = [("20-知识/项目记录/x.md", "", {"type": "project-record"})]
        assert not _collect_type_violations(notes, {"project-record"}, {})

    def test_alias_type_passes(self):
        """别名是登记过的存量写法，不该报 —— 否则 74 篇存量会把测试变成红海。"""
        notes = [("20-知识/项目记录/x.md", "", {"type": "project_record"})]
        assert not _collect_type_violations(
            notes, {"project-record"}, {"project_record": "project-record"}
        )

    def test_out_of_scope_type_ignored(self):
        """`10-项目/` 产物 type 不在类型文档管辖内（[[frontmatter-类型]] §1）。"""
        notes = [("10-项目/music/x/曲作.md", "", {"type": "composition"})]
        assert not _collect_type_violations(notes, {"project-record"}, {})

    def test_c_class_link_is_caught(self):
        text = '---\nupstream: "[[a]], [[b]]"\n---\n正文\n'
        notes = [("20-知识/项目记录/x.md", text, {"upstream": "[[a]], [[b]]"})]
        assert _collect_format_violations(notes, exempt=set())

    def test_double_frontmatter_is_caught(self):
        text = "---\nstatus: a\n---\n---\nproject: b\n---\n"
        notes = [("20-知识/项目记录/x.md", text, {"status": "a"})]
        assert _collect_format_violations(notes, exempt=set())

    def test_exempt_path_suppresses(self):
        text = '---\nupstream: "[[a]], [[b]]"\n---\n'
        notes = [("10-项目/pain-radar/PRD.md", text, None)]
        assert not _collect_format_violations(
            notes, exempt={"10-项目/pain-radar/PRD.md"}
        )

    def test_clean_note_passes_format(self):
        text = '---\ntype: project-record\nupstream: "[[a]]"\n---\n正文\n'
        notes = [("20-知识/项目记录/x.md", text, {"type": "project-record"})]
        assert not _collect_format_violations(notes, exempt=set())

    def test_list_type_is_caught(self):
        notes = [("20-知识/项目记录/x.md", "", {"type": ["a", "b"]})]
        assert _collect_list_type_offenders(notes)

    def test_missing_required_field_is_caught(self):
        notes = [("00-系统/仪表盘/x.md", "", {"type": "dashboard"})]  # 缺 title / created
        broken = _collect_required_violations(notes)
        assert broken and "title" in broken[0]

    def test_underscore_meta_entry_is_skipped(self):
        """`_config.md` 这类元条目有自己的 schema，不该按条目必填字段查。"""
        notes = [("00-系统/产物注册表/_config.md", "", {"type": "artifact-registry-config"})]
        assert not _collect_required_violations(notes)

    def test_complete_required_fields_pass(self):
        notes = [("00-系统/仪表盘/x.md", "", {
            "type": "dashboard", "title": "t", "created": "2026-08-16",
        })]
        assert not _collect_required_violations(notes)
