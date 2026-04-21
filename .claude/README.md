# AI 工作流系统文档

## 项目概述

本系统是一个**多智能体自治工作流框架**，由 Claude 驱动。通过角色分工、状态机调度和自我修复机制，将产品需求（PRD）自动转化为完整的代码实现。

---

## 目录结构

```
workflow/                          ← 项目根目录（PROJECT_ROOT）
└── .claude/
    ├── README.md                  ← 本文件
    ├── status.json                ← 系统状态（运行时更新）
    ├── audit.jsonl                ← 审计日志（追加写入）
    ├── 状态说明.md                ← status.json 字段说明
    │
    ├── skills/                    ← 技能定义与执行层
    │   ├── common.py              ← 共享工具库（所有 main.py 依赖）
    │   ├── product_manager/
    │   │   ├── skill.md           ← 角色定义（system prompt 来源）
    │   │   └── main.py            ← 执行入口
    │   ├── chief_architect/
    │   │   ├── skill.md
    │   │   └── main.py
    │   ├── technical_lead/
    │   │   ├── skill.md
    │   │   └── main.py
    │   ├── dev_backend/
    │   │   ├── skill.md
    │   │   └── main.py
    │   └── dev_frontend/
    │       ├── skill.md
    │       └── main.py
    │
    ├── script/
    │   ├── workflow.py            ← 单技能调度器（状态机 + 自愈补丁）
    │   ├── optimize_all.py        ← 全链路批量执行
    │   └── optimize_all_workflow说明.md
    │
    ├── docs/
    │   ├── tech_stack.md          ← 技术栈规范（禁止擅自变更）
    │   ├── rules/
    │   │   └── arch_decomposition_rules.md  ← 架构分解方法论
    │   ├── system_design.md       ← 运行时生成（chief_architect 输出）
    │   └── api_spec.md            ← 运行时生成（dev_backend 输出）
    │
    ├── inputs/                    ← 脑暴素材目录（product_manager 的输入源）
    │   ├── README.md                      ← 使用说明 + 命名约定
    │   ├── business_brief.example.md      ← 业务简报模板
    │   ├── business_brief.md              ← （用户可选）核心简报
    │   ├── brainstorm-*.md                ← （用户可选）brainstorming 产出 / 其他模型脑暴
    │   ├── meeting-*.md                   ← （用户可选）会议纪要 / 用户访谈
    │   ├── research-*.md                  ← （用户可选）用户/市场调研
    │   └── competitor-*.md                ← （用户可选）竞品分析
    │
    ├── requirements/
    │   └── PRD.md                 ← 运行时生成（product_manager 输出，含『参考资料』章节相对链接回 ../inputs/）
    │
    └── instructions/              ← 技能间任务传递（运行时生成）
        ├── to_lead.md             ← chief_architect → technical_lead
        ├── to_backend.md          ← technical_lead → dev_backend
        └── to_frontend.md         ← technical_lead → dev_frontend

（运行后在 workflow/ 下生成）
├── src/
│   ├── backend/                   ← dev_backend 生成的代码
│   └── frontend/                  ← dev_frontend 生成的代码
└── tests/
    ├── backend/
    └── frontend/
```

---

## 技能调用链

```
[输入] inputs/*.md（business_brief / brainstorm-* / meeting-* / research-* / ...）+ TASK
          ↓
  product_manager/main.py
  ├─ 读取：inputs/ 下全部 .md（综合多份素材）, tech_stack.md, status.json
  └─ 输出：requirements/PRD.md（末尾的『参考资料』章节用相对链接指回 ../inputs/）
          ↓
  chief_architect/main.py
  ├─ 读取：PRD.md, tech_stack.md, arch_decomposition_rules.md, status.json
  └─ 输出：docs/system_design.md, instructions/to_lead.md
          ↓
  technical_lead/main.py
  ├─ 读取：to_lead.md, system_design.md, tech_stack.md
  │  + 注入：chief_architect/skill.md 的动态补丁
  └─ 输出：instructions/to_backend.md, instructions/to_frontend.md
          ↓                    ↓
  dev_backend/main.py    dev_frontend/main.py
  ├─ 读取：to_backend.md  ├─ 读取：to_frontend.md
  │  system_design.md    │  PRD.md, system_design.md
  │  + technical_lead 补丁  + technical_lead 补丁
  └─ 输出：src/backend/   └─ 输出：src/frontend/
           docs/api_spec.md
```

**下游监控传递**：`product_manager` 监控 `chief_architect`，`chief_architect` 监控 `technical_lead`，`technical_lead` 监控 `dev_backend/dev_frontend`。连续失败时上游可向下游 `skill.md` 的 DYNAMIC 区域注入补丁。

---

## 快速开始

### 前置条件

```bash
pip install anthropic
export ANTHROPIC_API_KEY="your-api-key"
```

### 运行方式

```bash
cd workflow

# 首次使用：把脑暴素材放到 inputs/ 目录
# 方式 A：复制业务简报模板，填写核心需求
cp .claude/inputs/business_brief.example.md .claude/inputs/business_brief.md

# 方式 B：把 superpowers brainstorming skill 的产出直接保存到 inputs/
#   → .claude/inputs/brainstorm-mvp-scope.md

# 方式 C：多份文件综合（简报 + 会议纪要 + 竞品调研...）
#   → .claude/inputs/meeting-2026-04-20.md
#   → .claude/inputs/competitor-trello.md

# 方式一：运行完整链路（推荐）
TASK="任务管理系统 MVP" python .claude/script/optimize_all.py

# 方式二：单独运行某个技能（测试用）
TARGET_SKILL=product_manager TASK="任务管理系统 MVP" \
  python .claude/script/workflow.py

# 方式三：直接调用某个技能的 main.py（调试用）
python .claude/skills/product_manager/main.py --task "任务管理系统 MVP"
python .claude/skills/chief_architect/main.py --task "按 PRD 架构分解"
```

### 环境变量

| 变量名 | 必须 | 默认值 | 说明 |
|--------|------|--------|------|
| `ANTHROPIC_API_KEY` | ✓ | — | Anthropic API 密钥 |
| `TARGET_SKILL` | — | `chief_architect` | workflow.py 的目标技能 |
| `TASK` | — | `处理数学分析` | 传给技能的任务描述 |
| `MAX_ITER` | — | `3` | workflow.py 最大重试次数 |
| `SKILL_TIMEOUT` | — | `300` | 子进程超时秒数 |
| `SET_GLOBAL_BLOCK` | — | `false` | 技能 blocked 时是否阻塞系统 |

---

## 核心机制

### 1. 自愈补丁循环（workflow.py）

```
运行技能 main.py
    ↓ 失败
备份 skill.md → 生成补丁 → 注入 DYNAMIC 区域 → 重试
    ↓ 连续失败 ≥ 2 次
技能状态 → blocked（等待上级干预）
```

### 2. 动态补丁区域（skill.md）

每个 `skill.md` 末尾包含动态区域，用于运行时注入优化指令：

```markdown
<!-- DYNAMIC_START -->
# Patch [2026-03-21T10:00:00Z]:
- 所有数据库查询必须使用参数化查询，防止 SQL 注入。
<!-- DYNAMIC_END -->
```

- 上游技能的补丁会在下游技能的 `main.py` 中自动读取并追加到 system prompt
- 补丁基于 SHA256 哈希去重，不会重复注入

### 3. 多文件输出协议

Claude 的输出必须使用以下标签格式写入文件：

```
<!-- FILE: src/backend/main.py -->
# 代码内容
<!-- /FILE -->
```

`common.py` 的 `parse_claude_output_to_files()` 负责解析并批量写入，若无 FILE 标签则降级写入默认文件。

### 4. 状态机

```
idle → busy → success → idle      （正常流程）
             ↘ failed → busy      （重试）
                      ↘ blocked   （需人工介入）
```

`monitoring` 状态仅用于 `chief_architect`，始终保持不变。

---

## 扩展技能

在 `skills/` 下新增目录并创建两个文件即可：

**1. `skills/新技能名/skill.md`** — 定义角色职责、输入输出、禁止事项

**2. `skills/新技能名/main.py`** — 参照现有 `main.py`，修改：
- `SKILL_NAME`
- `input_files` 列表
- `user_prompt` 中的任务说明
- `output_files` 降级写入路径

**3. 注册到 `status.json`** — 在 `skill_registry` 中添加条目

**4. 按需加入 `optimize_all.py` 的 `skills_chain`**

---

## 常见问题

**Q: 运行后没有生成 src/ 目录下的代码？**
检查 `instructions/to_backend.md` 和 `instructions/to_frontend.md` 是否已生成（需先运行 chief_architect 和 technical_lead）。

**Q: 技能状态变成了 blocked？**
查看 `audit.jsonl` 了解失败原因，手动将 `status.json` 中该技能的 `status` 改为 `idle`，`consecutive_failures` 改为 `0` 后重试。

**Q: Claude 输出没有 FILE 标签？**
系统会降级写入默认文件（如 `src/backend/output.py`），此时可检查降级文件内容，或调整 system prompt 中的输出格式规范后重试。

**Q: API 调用超时？**
增大 `SKILL_TIMEOUT` 环境变量（默认 300 秒）：`SKILL_TIMEOUT=600 python .claude/script/optimize_all.py`
