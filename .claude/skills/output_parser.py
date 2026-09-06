"""
output_parser.py — Claude 输出解析与原子写入（单一职责：输出处理）

职责：
- parse_claude_output_to_files：解析 <!-- FILE: --> 标签
- parse_claude_output_to_calls：解析 <!-- CALL: --> 标签（capability 调用请求）
- write_output_atomic：原子写入文件（带 Windows 重试）
- _strip_outer_code_fence / _normalize_empty_file_placeholder：内部辅助

不依赖 LLM / API 调用，可独立测试。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.obsidian_io import _atomic_replace_with_retry  # noqa: E402


# ── 原子写入 ──────────────────────────────────────────────
def write_output_atomic(dest_path: Path, content: str) -> None:
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=dest_path.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
        newline="\n",
    ) as tf:
        tf.write(content)
        tmp = tf.name
    _atomic_replace_with_retry(tmp, dest_path)


# ── 输出解析 ──────────────────────────────────────────────
_FILE_BLOCK_RE = re.compile(
    r"<!--\s*FILE:\s*(.+?)\s*-->\n(.*?)<!--\s*/FILE\s*-->",
    re.DOTALL,
)

# 只匹配起始 marker，用于两件事：
#   1. 统计 LLM **声明**了多少个块（与实际解析出的数量比对，抓未闭合块）
#   2. 在已捕获的块内容里找残留 marker（抓被吞掉的下一个块）
_FILE_START_RE = re.compile(r"<!--\s*FILE:\s*(.+?)\s*-->")

_LEADING_FENCE_RE = re.compile(r"\A\s*```[^\n`]*\n")
_TRAILING_FENCE_RE = re.compile(r"\n```\s*\Z")
_PURE_COMMENT_RE = re.compile(r"\A\s*(?:<!--.*?-->\s*)+\Z", re.DOTALL)


def _strip_outer_code_fence(content: str) -> str:
    """若 content 整体被一对 markdown 代码围栏包裹，剥离外层。"""
    head = _LEADING_FENCE_RE.search(content)
    tail = _TRAILING_FENCE_RE.search(content)
    if not head or not tail:
        return content
    inner = content[head.end():tail.start()]
    return inner if inner.endswith("\n") else inner + "\n"


def _normalize_empty_file_placeholder(content: str) -> str:
    """若 content 仅包含 HTML/markdown 注释（无实际代码），写空文件。"""
    if _PURE_COMMENT_RE.match(content):
        return ""
    return content


def _split_residual_blocks(
    rel: str, body: str, warnings: list[str]
) -> list[tuple[str, str]]:
    """在已捕获的块内容里切出被吞掉的后续块。

    触发条件：LLM 漏写 `<!-- /FILE -->`。此时 non-greedy 正则会一路吃到**下一个**
    块的闭合标签，于是下一个块的起始 marker + 全部内容都被算进上一个文件。
    后果是上一个文件带着残留 marker（进 .py 直接 SyntaxError），下一个文件**静默消失**。

    实战：pain-radar 坑 8（2026-05-16）—— T06a 两个 FILE 块拼进一个文件，
    `test_radar.py` 744 行手工删到 597 行才干净。
    """
    starts = list(_FILE_START_RE.finditer(body))
    if not starts:
        return [(rel, body)]

    warnings.append(
        f"`{rel}` 的内容里发现 {len(starts)} 个残留 FILE marker"
        f"（上游漏写 `<!-- /FILE -->`），已按 marker 位置切分恢复："
        + "、".join(f"`{s.group(1).strip()}`" for s in starts)
    )
    out = [(rel, body[: starts[0].start()])]
    for i, s in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        out.append((s.group(1).strip(), body[s.end():end].lstrip("\n")))
    return out


def parse_claude_output_to_files(raw_output: str) -> dict:
    """解析 Claude 输出中的 <!-- FILE: path --> ... <!-- /FILE --> 块。

    返回 {相对路径: 内容}。注意路径中的 {project} 占位符不在此处替换，
    由调用方在写盘前用 engine.config.resolve_path 处理。
    自动剥离整体被 markdown 代码围栏包裹的内容（Claude 偶尔违反约定）。

    ## 鲁棒性（2026-08-16 补，pain-radar 坑 8 + mini-ledger §3 驱动）

    三种**静默丢文件**的失效模式，本函数一律改为「尽力恢复 + stderr 告警」，
    不再无声吞掉：

    1. **漏写闭合标签** → 下一个块被吞进上一个文件。切分恢复，见
       `_split_residual_blocks`。
    2. **输出被 max_tokens 截断** → 最后一个块没有闭合标签，正则**完全不匹配**，
       该文件凭空消失且无任何迹象。用「声明块数 vs 恢复块数」比对抓出来。
       （CLI 路径拿不到 API 的 `stop_reason`，只能从输出形态反推。）
    3. **同一路径出现多次** → 后者静默覆盖前者。pain-radar 坑 4 的两份
       `radar.py` 即此类。

    告警走 stderr，不改返回类型（所有调用方都只吃 dict）。**不判失败** ——
    这些都是可恢复的格式问题，判失败要多烧一次 LLM 调用且未必更好；
    但必须可见，否则就是上面三条能潜伏三个月的原因。
    """
    results: dict[str, str] = {}
    warnings: list[str] = []
    recovered = 0

    for m in _FILE_BLOCK_RE.finditer(raw_output):
        for rel, body in _split_residual_blocks(
            m.group(1).strip(), m.group(2), warnings
        ):
            recovered += 1
            content = _normalize_empty_file_placeholder(_strip_outer_code_fence(body))
            if rel in results:
                warnings.append(
                    f"`{rel}` 出现多次，后者覆盖前者"
                    f"（{len(results[rel])} → {len(content)} chars）—— "
                    f"若两块是同一文件的上下半段，本次会丢掉上半段"
                )
            results[rel] = content

    declared = len(_FILE_START_RE.findall(raw_output))
    if declared > recovered:
        warnings.append(
            f"声明了 {declared} 个 FILE 块但只恢复出 {recovered} 个 —— "
            f"最可能是输出被 max_tokens 截断（末块无闭合标签），"
            f"缺失的文件不会落盘"
        )

    for w in warnings:
        print(f"[parse_output] ⚠️ {w}", file=sys.stderr)
    return results


# ── capability 调用块解析（3c · 2026-09-06）──────────────────
#
# 背景：capability_executor 的 executor / sandbox / audit 全套早就写好了，缺的
# 只是一个调用点 —— 角色跑在 api 轨（无 tool use）或 cli 轨（--disallowed-tools
# 封了 Bash），两条轨都执行不了摘要里宣传的那条 CLI。于是「已注册的能力」从来
# 没被任何角色调用过。
#
# 修法不是给角色发工具权限（要动安全线），而是让角色**吐一个调用块**、引擎代它
# 执行 —— 与 FILE 块完全同构，provider 无关（deepseek/gemini/ollama 一起生效）。
_CALL_BLOCK_RE = re.compile(
    r"<!--\s*CALL:\s*(.+?)\s*-->\n(.*?)<!--\s*/CALL\s*-->",
    re.DOTALL,
)
_CALL_START_RE = re.compile(r"<!--\s*CALL:\s*(.+?)\s*-->")

# invoke_capability 会把输入插进命令行模板，只接受标量。
_SCALAR_TYPES = (str, int, float, bool)


@dataclass(frozen=True)
class CapabilityCall:
    """角色输出里的一个 capability 调用请求。

    `error` 非 None = 块本身有问题（YAML 挂 / 值不是标量 / 块体不是映射）。
    **带错的块照样返回，不在解析层丢弃** —— 调用方要把它写进 audit。
    静默跳过格式不对的调用请求，正是本仓一直在修的那类失效：角色以为调了、
    实际没调、产物里看不出来。
    """
    capability_id: str
    inputs: dict = field(default_factory=dict)
    error: str | None = None


def parse_claude_output_to_calls(raw_output: str) -> list[CapabilityCall]:
    """解析 <!-- CALL: <id> --> ... <!-- /CALL --> 块。

    块体是 YAML 映射（选 YAML 而非 `k: v` 单行，是因为 huashu-design 的
    `design_brief` 是 3-5 段文本，必须支持 `|` 块标量）：

        <!-- CALL: huashu-design/render-prototype -->
        design_brief: |
          做一个简约深色主题的 iOS 待办 App 首页原型。
          顶部渐变导航栏 + 卡片式列表 + 右下悬浮 + 按钮。
        artifact_path: 10-项目/{project}/交付物/prototype.html
        <!-- /CALL -->

    **先挖掉 FILE 块再扫**：角色在产物正文里举例说明调用格式是完全合理的写法，
    那种 CALL 标记是文档内容，不是调用请求。不挖的话，一份"怎么调用能力"的
    说明文档会把自己描述的调用真的执行一遍。

    与 `parse_claude_output_to_files` 同款的截断检测：声明块数 > 恢复块数
    说明末块没闭合（多半是 max_tokens 截断），走 stderr 告警。
    """
    scan_area = _FILE_BLOCK_RE.sub("", raw_output)

    calls: list[CapabilityCall] = []
    warnings: list[str] = []

    for m in _CALL_BLOCK_RE.finditer(scan_area):
        cid = m.group(1).strip()
        body = m.group(2)
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as e:
            calls.append(CapabilityCall(cid, error=f"块体 YAML 解析失败：{e}"))
            continue
        if data is None:
            data = {}
        if not isinstance(data, dict):
            calls.append(CapabilityCall(
                cid,
                error=f"块体必须是 YAML 映射（`输入名: 值`），实际解析出 "
                      f"{type(data).__name__}",
            ))
            continue
        bad = {
            k: type(v).__name__ for k, v in data.items()
            if not isinstance(v, _SCALAR_TYPES)
        }
        if bad:
            calls.append(CapabilityCall(
                cid,
                error=f"输入值必须是标量（str/int/float/bool），"
                      f"以下不是：{bad}",
            ))
            continue
        calls.append(CapabilityCall(cid, {str(k): v for k, v in data.items()}))

    declared = len(_CALL_START_RE.findall(scan_area))
    if declared > len(calls):
        warnings.append(
            f"声明了 {declared} 个 CALL 块但只恢复出 {len(calls)} 个 —— "
            f"最可能是输出被 max_tokens 截断（末块无 `<!-- /CALL -->`），"
            f"缺失的调用不会执行"
        )

    for w in warnings:
        print(f"[parse_output] ⚠️ {w}", file=sys.stderr)
    return calls
