"""
technical_lead/_module_manifest_prompt.py — P8.7 module_manifest 模式的 user_prompt 构造。

隔离到独立文件的原因：user_prompt 含大段 markdown / yaml / mermaid 样板，
放在 main.py 里会因转义和 encoding 变得非常脆弱。这里用三引号原文即可。

调用方：technical_lead/main.py::_run_module_manifest_mode
"""

from __future__ import annotations


_SAMPLE_TEMPLATE = """<!-- FILE: 10-项目/{project}/模块清单.md -->
# 模块清单

## 概览
（一段简述模块拆解思路 + 依赖关系）

## 结构化（DAG 原始数据，引擎消费）

```yaml
nodes:
  - id: T01
    title: 数据模型
    role: backend
    depends_on: []
    status: pending
    estimate_hours: 2
  - id: T02
    title: CRUD 路由
    role: backend
    depends_on: [T01]
    status: pending
    estimate_hours: 3
```

## 拓扑（Mermaid）

```mermaid
graph LR
  T01 --> T02
```
<!-- /FILE -->

<!-- FILE: 10-项目/{project}/模块/T01-数据模型.md -->
# 模块 T01：数据模型

## 目标
（本模块交付的可运行/可测能力）

## 输入
- 上游模块产物
- 系统设计相关章节

## 输出
- `src/backend/models.py`
- `tests/backend/test_models.py`

## 验收
- pytest tests/backend/test_models.py 全绿
- 边界条件覆盖

## 失败模式
- 场景 + 独占降级路径
<!-- /FILE -->"""


_PROMPT_TEMPLATE = """**本轮 output_contract.artifacts_pattern = module_manifest**（模块化开发工作流）。

本段即该模式的**完整产出契约**。角色 md §5 只在步骤 3 指出存在本分支，不复述细节 ——
2026-08-16 前两处各存一份，既让 legacy 模式白吃 2795 chars，又埋下双份漂移的隐患。

### 执行上下文（关键，务必先读）

你正被一个**自动化 Python 管道**（skills/technical_lead/main.py）调用。输出通过正则 `<!-- FILE: ... -->...<!-- /FILE -->` 直接解析并落盘，**没有人工审批、没有弹框、没有 CLI 交互**。

禁止输出以下类型内容（属于错误的对话模式，会污染解析）：
- ❌「vault 写入需要弹框批准」/「请点击批准」/「审批模式:cli」/「手动落盘」/「需要你确认」
- ❌「让我尝试再次写入」/「以上是我的产出」/「以下是 N 个 FILE 块」等前言/后记
- ❌ 询问用户偏好 / 请求补充信息（契约已定，直接产出）

### FILE 块格式（唯一合法样式）

**必须**用 HTML 注释 marker 包裹（不是 markdown 标题）：

```
<!-- FILE: 完整相对路径.md -->
（此文件的完整正文，可含 markdown / yaml / mermaid 等代码块）
<!-- /FILE -->
```

**错误示例**（会导致解析失败 → 整轮 fail）：
- ❌ 用 `## FILE: path` / `# FILE: path` markdown 标题当分隔符
- ❌ 用 ` ```markdown ... ``` ` / ` ```yaml ... ``` ` 包裹**整个** FILE 块外层（内部允许保留代码围栏）
- ❌ 缺 `<!-- /FILE -->` 结尾 marker

### 需产出的 FILE 块清单

1. `10-项目/{project}/模块清单.md` — **必产 1 份**
2. `10-项目/{project}/模块/<module_id>-<title_slug>.md` — **每模块 1 份**（N ≥ 1）

**禁止产出的 legacy 路径**（本轮契约已禁用）：
- ❌ `10-项目/{project}/指令/给后端-T0N.md` / `给前端-T0N.md`
- ❌ `10-项目/{project}/指令/给后端-索引.md` / `给前端-索引.md`

### 「模块清单.md」结构硬约束（引擎 manifest_validator fail-closed，违反即 ManifestValidationError）

- 必含 `## 结构化（DAG 原始数据，引擎消费）` H2 段 + 内嵌一个 ` ```yaml``` ` 块
- 顶层 `nodes:` 是 list，至少 1 项
- 每 node 必填：`id` / `title` / `role` / `depends_on` / `status`；**缺一即 fail**
- `id` 全局唯一，禁止重复；建议 `T01/T02/...` 或 `M1a/M1b/...` 命名
- `title` 为模块简短标题，**8-16 字**
- `## 概览` 段只写拆解思路 + 依赖概览，**不要复述 PRD / 系统设计原文，指向即可**
- `role` ∈ `{{backend, frontend}}`（其它值拒载）
- `status` 初始一律 `pending`（`in_progress` / `done` / `blocked` 由引擎在 engineer / gate 阶段改写）
- `depends_on` 是 list，可为空 `[]`；引用的每个 id **必须在 nodes 里存在**
- **禁止自依赖、禁止环**（Kahn 拓扑排序失败即 fail）
- 允许可选字段 `estimate_hours` / `user_story` / `notes`（不影响校验）
- 建议加 `## 拓扑（Mermaid）` 段便于 Obsidian 渲染

### title_slug 规则（`模块/<module_id>-<title_slug>.md` 用）

- 从 title 生成，保留中文，去空格与 `/ \\ : * ? " < > |` 特殊字符，长度 ≤ 30 字符
- 例：title="数据模型 & 校验" → slug="数据模型-校验"

### 「模块/<module_id>-<title_slug>.md」结构

每个模块详情文件包含 5 段（下游 engineer skill 消费）：

```markdown
# 模块 <module_id>：<title>

## 目标
（一段：本模块要交付什么可运行/可测的能力。绑定 PRD 用户故事 US-xx / 系统设计 §x.x。）

## 输入
- 上游模块产物（列 `depends_on` 模块的输出文件/接口）
- 系统设计里本模块相关章节（引用式，如 `参见系统设计.md §3.2`，不复述）
- 需读取的 vault 文件（如 `10-项目/{project}/PRD.md` 相关章节）

## 输出
- 代码文件（列 `src/backend/...` 或 `src/frontend/...` 具体路径）
- 测试文件（`tests/backend/...` 或 `tests/frontend/...`）
- 可能的文档更新（如 README 段）

## 验收
- 3-6 条独立可执行的验收项（每条含"命令 / 条件 → 期望"）
- 至少含 1 条 pytest / 手动测试可复现步骤
- 边界条件覆盖（空 / 超长 / 非法输入 / 依赖异常）

## 失败模式
- 3-5 条本模块可能遇到的失败场景 + 独占降级路径（[[A2-失败模式表穷举非框架异常]] + [[A4-降级路径独占覆盖]]）
- 禁止 `try/except: pass` 兜底
```

### 边界与禁止

- **禁止**用 Plan+Detail×N 两阶段流程（本模式一次性输出所有 FILE 块）
- **禁止**为多个模块共用一个详情文件（引擎按 `模块/<module_id>-*.md` 通配加载单模块聚焦）

### 拆解建议

- 全部模块（前后端）综合拆解，按 `role` 字段区分
- 若项目纯后端（如 CLI / API-only），可全部 `role: backend`；含 UI 则至少 1 个 `role: frontend`
- 建议 3-8 个模块（≤ 3 太粗、≥ 8 过细）

### 输出样板（照抄格式，替换内容）

```
{sample}
```
"""


def build_module_manifest_user_prompt(project: str, base_prompt: str) -> str:
    """构造 module_manifest 分支的 user_prompt。

    base_prompt 包含项目名 + 输入摘要 + 任务描述（由 main.py 组装）。
    本函数在 base_prompt 之后追加 module_manifest 分支的产出格式约束 + 样板。
    """
    sample = _SAMPLE_TEMPLATE.format(project=project)
    return base_prompt + _PROMPT_TEMPLATE.format(project=project, sample=sample)
