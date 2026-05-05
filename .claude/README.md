# agent-workflow 引擎仓

Obsidian-backed multi-agent 工作流编排引擎。本仓只放**代码**，所有"知识"（角色定义、工作流模板、规则、项目产出）在另一个 Obsidian vault 仓里。

> **用户视角**（如何启动新项目、怎么跑工作流、怎么换模型）请看 vault 内的 [`00-系统/启动新项目指南.md`](../../../MarkDown/memory/adam/00-系统/启动新项目指南.md)（具体路径取决于你的 `VAULT_ROOT` 配置）。
>
> 本 README 是**开发者视角**：引擎模块如何组织、扩展工作流要改哪里、调试入口在哪。

## 目录结构

```
.claude/
├── engine/              ← 引擎核心（无业务，全是基础设施）
│   ├── config.py        ← 加载 .env / VAULT_ROOT / PROJECT_ROOT / 路径解析
│   ├── obsidian_io.py   ← vault 文件读写（filesystem-only）+ Windows 文件锁重试
│   ├── role_loader.py   ← 解析 vault 角色笔记 → Role dataclass
│   ├── runtime_state.py ← 角色运行时状态 per-role JSON（gitignored 在 vault）
│   ├── state.py         ← 状态机语义层（idle/busy/success/failed/blocked/monitoring）
│   ├── llm.py           ← provider-agnostic LLM 调用（Anthropic SDK / OpenAI 兼容 / CLI）
│   ├── llm_providers.yaml ← LLM 配置：model 名 → API/CLI 双轨设置
│   ├── git_sync.py      ← agent 分支约定 + commit/push/PR
│   ├── workflow.py      ← 工作流模板加载 + 角色名 → skill 目录映射
│   ├── run_chain.py     ← CLI 入口：按模板顺序跑链路
│   └── _smoke_test.py   ← 集成自检
└── skills/              ← 角色执行器（run_chain.py 的子进程目标）
    ├── common.py        ← 共享工具：build_system_prompt / call_claude / FILE 块解析
    ├── product_manager/main.py
    ├── chief_architect/main.py
    ├── technical_lead/main.py
    ├── dev_backend/main.py
    └── dev_frontend/main.py
```

## 数据流

```
   vault                                       project repo (本仓)
   ─────                                       ──────────────────
   00-系统/角色基因/      ── load_role ──>     engine/role_loader.py
   00-系统/规则/          ── read_note ──>     skills/main.py
   00-系统/工作流模板/    ── load_workflow ──> engine/workflow.py
                                                    │
   10-项目/{p}/inputs/   ── 读 ──>  ┌──────────────▼──────────────┐
                                    │  skills/<role>/main.py       │
   00-系统/.runtime-state/<role>.json   │  (subprocess from run_chain)│
                          ↑↓ 读写       │                              │
                                    │  engine.llm.call_llm()       │
                                    └──────────────┬──────────────┘
                                                    │
   10-项目/{p}/PRD.md  <── write_note ──┐         │
   10-项目/{p}/系统设计.md             │         │
   10-项目/{p}/指令/给*.md             ◄─────────┘
   10-项目/{p}/API契约.md              │
                                        │
   src/backend/, src/frontend/, tests/  ◄── 项目仓内（gitignored）
```

## 入口

```bash
# 跑一条完整链路（默认 "技术开发" 工作流）
python .claude/engine/run_chain.py --task "..." --project myproj

# 列出所有可用工作流
python .claude/engine/run_chain.py --list-workflows

# 自检（不调 Claude）
python .claude/engine/_smoke_test.py
```

## 扩展指引

### 新增 LLM provider
改 [engine/llm_providers.yaml](engine/llm_providers.yaml) — 加一条目（mode / api / cli），代码无需改。

### 新增角色
在 vault `00-系统/角色基因/` 新建一份 `角色-XX.md`，按现有 frontmatter schema 写。然后在 `.claude/skills/` 新建一个 `<英文名>/main.py` 作为执行器（参考已有 5 份的结构）。

### 新增工作流
在 vault `00-系统/工作流模板/` 新建 `工作流-XX.md`，frontmatter 里写 `steps` 列表。引擎 `--workflow XX` 即可调用。

## 配套约定

- **vault git 工作流**：vault 仓的 main 分支受保护（本地 hook + agent 分支 + 手动 PR）。详见 vault 仓 `README.md`
- **运行时状态**：不进 git；vault `.runtime-state/` 已 gitignored
- **工程测试产物**：放 vault `99-临时/test-runs/<name>/`（已 gitignored），不污染 `10-项目/`

## Phase 路线图

- **Phase 1-3a：完成** — vault + engine + 工作流模板系统
- **Phase 3b（下一步）**：跨领域角色（自媒体/生活）+ 跨域工作流模板
- **Phase 4**：LangGraph 编排（讨论循环、并行）+ 复盘 agent（替代旧 self-healing）
- **Phase 5**：Obsidian Canvas 实时仪表盘 + meeting-chat 退役
