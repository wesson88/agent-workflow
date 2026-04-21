# optimize_all.py & workflow.py 说明文档

## optimize_all.py

按固定链条顺序（chief_architect → technical_lead → dev_backend → dev_frontend）依次调用 `workflow.py`，实现从 PRD 到代码的完整端到端执行。

- 任意一个技能失败时立即终止，不继续执行后续技能。
- 每个技能通过 `TARGET_SKILL` 环境变量传给 `workflow.py`。

**用法：**
```bash
cd workflow
python .claude/script/optimize_all.py
```

---

## workflow.py

单个技能的调度器，负责：状态机管理、调用技能 `main.py`、失败时生成补丁并重试、写审计日志。

- `run_skill()` 调用 `python {技能目录}/main.py --task {TASK}`
- 最多重试 `MAX_ITER` 次（默认 3）
- 连续失败 ≥ `consecutive_failures_limit`（默认 2）次后，技能状态置为 `blocked`
- 子进程超时由 `SKILL_TIMEOUT` 环境变量控制（默认 300 秒）

**用法：**
```bash
TARGET_SKILL=chief_architect TASK="架构分解任务管理系统" \
  python .claude/script/workflow.py
```

**环境变量：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TARGET_SKILL` | `chief_architect` | 目标技能名称 |
| `TASK` | `处理数学分析` | 传给 main.py 的任务描述 |
| `MAX_ITER` | `3` | 最大重试次数 |
| `SKILL_TIMEOUT` | `300` | 子进程超时秒数 |
| `SET_GLOBAL_BLOCK` | `false` | blocked 时是否阻塞整个系统 |
| `TEST_COMMAND` | _(空)_ | 自定义测试命令（替代默认 main.py 调用） |

---

## 技能执行层（main.py）

每个技能目录下的 `main.py` 是真正的业务执行单元：

1. 读取自身 `skill.md` 作为 Claude 的 system prompt
2. 读取上游技能的动态补丁（`<!-- DYNAMIC_START -->` 区域）并注入 system prompt
3. 合并输入文件（PRD、instruction、system_design 等）作为 user prompt
4. 调用 Claude API（streaming，模型：claude-sonnet-4-6）
5. 解析输出中的 `<!-- FILE: 路径 -->...<!-- /FILE -->` 块并写入对应文件
6. 更新 `status.json` 和 `audit.jsonl`

**直接调用（调试用）：**
```bash
python .claude/skills/chief_architect/main.py --task "架构分解任务管理系统"
```

---

## 公共工具库（skills/common.py）

所有 `main.py` 共享的工具函数，包括：路径管理、skill.md 读取、动态补丁提取、Claude API 调用（streaming）、多文件输出解析、原子写入、status.json 操作、审计日志追加。
