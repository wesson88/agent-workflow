# 多Agent智能会议聊天系统 - 完整文档

## 目录

1. [系统架构](#1-系统架构)
2. [快速上手](#2-快速上手)
3. [LLM Provider 配置](#3-llm-provider-配置)
4. [Agent 配置](#4-agent-配置)
5. [通用双轨策略](#5-通用双轨策略)
6. [添加新 Provider](#6-添加新-provider)
7. [添加新 Agent](#7-添加新-agent)
8. [切换 Agent 的模型/Provider](#8-切换-agent-的模型provider)
9. [Token 优化策略](#9-token-优化策略)
10. [运行集成测试](#10-运行集成测试)
11. [故障排查](#11-故障排查)
12. [变更日志](#12-变更日志)

---

## 1. 系统架构

```
用户消息
  │
  ▼
IntelligentGateway.route_message()
  ├── @mention 直接路由（无 LLM 调用，零延迟）
  └── 主持人 LLM 路由（精简 prompt，低温度，max_tokens=80）
         │
         ▼
  generate_agent_response()   ← 按 Agent 配置调用对应 Provider
         │
         ├── DeepSeek  (openai_compat)
         ├── Claude    (dual_track: API Key ↔ CLI 自动切换)
         ├── Codex     (openai_compat)
         ├── Gemini    (openai_compat 或 dual_track)
         └── 任意新 Provider（YAML 配置，无需改代码）
         │
         ▼
  generate_moderator_summary()  ← 主持人汇总（截断+精简，max_tokens=200）
```

**核心文件**

| 文件 | 职责 |
|------|------|
| `llm_providers.yaml` | 所有 Provider 定义（模型、接入模式、参数） |
| `agents.yaml` | 所有 Agent 定义（角色、Provider 绑定、Prompt） |
| `.env` | API Key 及环境变量（不提交到 git） |
| `config.py` | YAML 加载器，将 `${VAR}` 替换为环境变量 |
| `gateway.py` | `IntelligentGateway` 编排层 |
| `core/token_utils.py` | Token 估算、历史裁剪、文本截断 |
| `core/routing.py` | `@mention` 解析、路由规则常量 |
| `providers/cli_api_router.py` | `CliApiRouter` 双轨路由器（API ↔ CLI） |
| `main.py` | FastAPI 入口，WebSocket 端点 |
| `check_config.py` | 配置验证脚本，展示所有 Provider/Agent 状态 |
| `tests/test_integration.py` | 6层集成测试套件 |

**启动脚本**

| 文件 | 说明 |
|------|------|
| `start_all.ps1` | 一键后台启动前后端，脚本执行完自动退出，日志写入 `logs/` |
| `stop_all.ps1` | 停止所有后台服务（按 PID 精确停止 + 按端口兜底） |
| `logs/backend.log` | 后端运行日志（自动生成） |
| `logs/frontend.log` | 前端运行日志（自动生成） |

---

## 2. 快速上手

### 安装依赖

```powershell
cd meeting-chat\backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 配置 API Key

编辑 `.env`，至少填写 `DEEPSEEK_API_KEY`（主持人/路由默认使用 DeepSeek）：

```dotenv
DEEPSEEK_API_KEY=sk-xxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1   # 注意必须加 /v1
DEEPSEEK_MODEL=deepseek-chat
```

### 验证配置

```powershell
.venv\Scripts\python.exe check_config.py
```

输出示例：
```
=== 所有 Provider 状态 ===
  [deepseek]  mode=openai_compat  model=deepseek-chat  ✓ Key已配置
  [claude]  mode=dual_track  prefer=auto  cli_fmt=stream_json
    api=✗  cli=✗  → ❌ 两条路径均不可用

=== Agent -> Provider 映射 ===
  moderator    🎙️  主持人    -> deepseek (openai_compat)
  architect    🏗️  架构师    -> claude (dual_track)

=== 双轨 Provider 激活路径 ===
  [claude] 实际使用: ⚠️ 不可用
```

### 启动服务

#### 一键后台启动（推荐）

```powershell
# 在 meeting-chat/ 根目录执行
.\start_all.ps1
```

- 前后端进程在**后台静默运行**，启动脚本执行完自动退出，不挂起任何窗口
- 日志实时写入 `logs/` 目录，PID 保存在 `logs/pids.txt`
- 执行完毕后自动打开浏览器

**实时查看日志：**
```powershell
Get-Content logs\backend.log  -Wait   # 实时追踪后端日志
Get-Content logs\frontend.log -Wait   # 实时追踪前端日志
```

**停止所有服务：**
```powershell
.\stop_all.ps1
```

> `stop_all.ps1` 会先按 PID 文件精确停止，再兜底按端口（8765 / 5173 / 5174）清理残留进程。

#### 单独启动（开发调试）

```powershell
# 后端（前台，可看实时日志）
cd backend
.venv\Scripts\python.exe -m uvicorn main:app --reload

# 前端（另开终端）
cd frontend
npm run dev
```

---

## 3. LLM Provider 配置

所有 Provider 在 `llm_providers.yaml` 中定义，**修改配置无需改代码**。

### 接入模式（mode）完整列表

| 模式 | 适用场景 | 说明 |
|------|----------|------|
| `openai_compat` | DeepSeek / Gemini / Codex / 任何兼容 OpenAI 的 API | 纯 API Key，用 `openai` SDK |
| `dual_track` | **推荐**：同时提供 API 和 CLI 的模型（Claude / Gemini 等） | API ↔ CLI 互为备用，`prefer` 控制优先级 |
| `cli_only` | 纯本地 CLI（Ollama 等离线场景） | 不需要 API Key |
| `anthropic_api` | *(旧别名)* 等同于 `openai_compat` | 向下兼容 |
| `claude_auto` | *(旧别名)* 等同于 `dual_track` + `prefer: auto` | 向下兼容 |
| `claude_cli` | *(旧别名)* 等同于 `dual_track` + `prefer: cli` | 向下兼容 |

### Provider 字段完整说明

```yaml
providers:
  my_provider:
    # ── 必填 ──────────────────────────────────
    mode: openai_compat      # 接入模式（见上表）
    base_url: https://...    # API 端点，必须以 /v1 结尾
    model: model-name        # 默认模型名

    # ── API Key 轨道（openai_compat / dual_track）──
    api_key: ${MY_API_KEY}   # 从 .env 读取，dual_track 时可为空

    # ── CLI 轨道（dual_track / cli_only 时填写）──
    prefer: auto             # auto | api | cli
    cli_path: my-cli         # CLI 可执行文件名（需在 PATH 中）
    cli_model: model-name    # CLI 调用时使用的模型名
    cli_output_format: plain # stream_json（Claude CLI）| plain（其他）

    # ── 通用参数 ──────────────────────────────
    timeout: 60              # 超时秒数
    max_tokens: 2000         # 默认最大输出 Token
    temperature: 0.7         # 默认温度
```

> ⚠️ `base_url` 必须包含路径前缀，例如 `https://api.deepseek.com/v1`，
> 不能写 `https://api.deepseek.com`（会导致 `'str' object has no attribute 'choices'` 错误）。

---

## 4. Agent 配置

所有 Agent 在 `agents.yaml` 中定义。

### Agent 字段说明

```yaml
my_agent:
  name: 我的Agent              # 显示名称（支持中文）
  avatar: "🤖"                 # 显示图标（emoji）
  color: "#6366f1"             # 消息气泡颜色（#RRGGBB）
  role: expert                 # moderator（主持人，唯一）| expert
  provider: deepseek           # 对应 llm_providers.yaml 中的 key
  temperature: 0.7             # 可选，覆盖 provider 默认值
  max_tokens: 2000             # 可选，覆盖 provider 默认值
  skill_augment: false         # 是否追加 skill.md 内容到 system prompt
  skill_id: my_skill           # skill_augment=true 时必填
  prompt: |                    # system prompt
    你是...
```

### 当前 Agent 列表

| Agent ID | 名称 | Provider (mode) | 职责 |
|----------|------|-----------------|------|
| `moderator` | 主持人 🎙️ | DeepSeek (openai_compat) | 消息路由、会议纪要 |
| `architect` | 架构师 🏗️ | Claude (dual_track) | 系统设计、技术方案 |
| `tech_lead` | 技术主管 🧑‍💼 | Claude (dual_track) | 任务拆解、开发协调 |
| `backend` | 后端工程师 ⚙️ | Claude (dual_track) | API、数据库、服务端 |
| `frontend` | 前端工程师 🎨 | Claude (dual_track) | React/Vue、UI 实现 |
| `product` | 产品经理 📋 | DeepSeek (openai_compat) | 需求分析、功能规划 |
| `qa` | 测试工程师 🔍 | Codex (openai_compat) | 测试策略、质量保障 |
| `ux` | UX设计师 ✏️ | Gemini (openai_compat) | 交互设计、UI 代码生成 |

---

## 5. 通用双轨策略

> **v2 变更**：原 `ClaudeRouter`（Claude 专属）已升级为 `CliApiRouter`（通用），
> 任何 Provider 只要在 YAML 中配置 `mode: dual_track` 即可获得双轨能力。

### 双轨逻辑

```
mode: dual_track
prefer: auto
         │
         ├── api_key 已填写 → 走 API 轨道（OpenAI 兼容接口）
         ├── cli_path 已安装 → 走 CLI 轨道（本地子进程）
         └── 两者均无 → 抛出友好错误，其他 Agent 不受影响
```

### prefer 策略说明

| prefer | 行为 |
|--------|------|
| `auto` | 有 API Key → API；无 Key 但有 CLI → CLI；均无 → 报错 |
| `api`  | 强制 API，无 Key 直接报错 |
| `cli`  | 优先 CLI，CLI 不可用时降级 API |

### CLI 输出格式（cli_output_format）

| 值 | 适用工具 | 说明 |
|----|----------|------|
| `stream_json` | Claude Code CLI | 逐行 JSON 流，格式 `{"type":"text","text":"..."}` |
| `plain` | Gemini CLI / Ollama / 其他大多数 CLI | 纯文本流，逐行回调 |

### Claude 配置示例

```yaml
# llm_providers.yaml
claude:
  mode: dual_track
  prefer: auto
  api_key: ${CLAUDE_API_KEY}       # 填写后走 API
  base_url: ${CLAUDE_BASE_URL}
  model: ${CLAUDE_MODEL}
  cli_path: ${CLAUDE_CLI_PATH}     # 安装 CLI 后自动启用
  cli_model: ${CLAUDE_CLI_MODEL}
  cli_output_format: stream_json
```

**API Key 方式**：
```dotenv
CLAUDE_API_KEY=sk-ant-xxxx
CLAUDE_BASE_URL=https://api.anthropic.com/v1
CLAUDE_MODEL=claude-opus-4-5
```

**CLI 方式（无需 API Key）**：
```powershell
npm install -g @anthropic-ai/claude-code
claude login
# .env 中保持 CLAUDE_API_KEY 为空，系统自动切换到 CLI
```

### 为任意 Provider 开启双轨（示例：Gemini CLI）

```yaml
# llm_providers.yaml
gemini:
  mode: dual_track
  prefer: auto
  api_key: ${GEMINI_API_KEY}
  base_url: ${GEMINI_BASE_URL}
  model: ${GEMINI_MODEL}
  cli_path: gemini                 # npm install -g @google/gemini-cli
  cli_model: gemini-2.5-pro
  cli_output_format: plain         # Gemini CLI 输出纯文本
  max_tokens: 8000
  temperature: 0.8
```

---

## 6. 添加新 Provider

**第一步**：在 `.env` 添加 API Key：

```dotenv
QWEN_API_KEY=sk-xxxx
```

**第二步**：在 `llm_providers.yaml` 添加定义：

```yaml
qwen:
  mode: openai_compat
  api_key: ${QWEN_API_KEY}
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model: qwen-max
  timeout: 60
  max_tokens: 2000
  temperature: 0.7
```

**第三步**（可选）：将某个 Agent 切换到新 Provider：

```yaml
# agents.yaml
product:
  provider: qwen
```

**第四步**：验证：

```powershell
.venv\Scripts\python.exe check_config.py
```

---

## 7. 添加新 Agent

在 `agents.yaml` 末尾添加：

```yaml
data_analyst:
  name: 数据分析师
  avatar: "📊"
  color: "#14b8a6"
  role: expert
  provider: deepseek
  temperature: 0.5
  prompt: |
    你是数据分析专家，擅长数据建模、BI 报表、A/B 测试分析。
    始终用中文回复。
```

同时在 `moderator` 的 prompt 路由规则中补充：

```
- 数据分析、指标、埋点 -> data_analyst
```

无需修改任何代码，重启服务后即生效。

---

## 8. 切换 Agent 的模型/Provider

### 切换单个 Agent 的 Provider

```yaml
# agents.yaml
architect:
  provider: deepseek   # 从 claude 改为 deepseek
```

### 切换 Provider 的模型

```yaml
# llm_providers.yaml
deepseek:
  model: deepseek-reasoner
```

或通过 `.env` 控制（推荐，方便环境隔离）：

```dotenv
DEEPSEEK_MODEL=deepseek-reasoner
```

### Agent 级别参数覆盖

```yaml
qa:
  provider: codex
  temperature: 0.1
  max_tokens: 4000
```

---

## 9. Token 优化策略

以下优化已内置，**无需手动操作**：

| 优化点 | 机制 | 节省量 |
|--------|------|--------|
| **路由精简 prompt** | 路由时使用 15 字 system + 规则表，不发完整 agent prompt | 每次路由省 ~250 input token |
| **路由 max_tokens 限制** | 路由输出 JSON 只需 80 token（原 300） | 每次路由省最多 220 output token |
| **历史按 Token 预算裁剪** | `_trim_history_by_tokens()`：短消息多保留，长消息少保留 | 省 20~50% 历史 token |
| **总结截断专家回复** | 专家回复截断到 400 字后进总结；总结 max_tokens=200 | 每次总结省 ~400+ token |
| **历史存储截断** | 历史中每条消息截断存储（用户≤500字，回复≤600字） | 长期使用效果最显著 |

**历史裁剪逻辑**（`_trim_history_by_tokens`）：
```
历史 [A(200tk) B(50tk) C(800tk) D(30tk) E(100tk)]
预算 600tk，强制保留最后2条 D+E(130tk)
→ 从旧到新填充：C(800tk) 超预算跳过，B(50tk) 加入，A(200tk) 加入
→ 实际发送：[A, B, D, E]
```

**各调用场景的 max_tokens 设置**：

| 场景 | max_tokens | 原因 |
|------|-----------|------|
| 路由决策 | 80 | JSON 输出极短 |
| 主持人总结 | 200 | 2句话纪要 |
| 普通 Agent | YAML 配置值（默认 1000） | 按需设置 |
| UX/Gemini | 8000 | 生成完整 UI 代码 |

---

## 10. 运行集成测试

```powershell
# 运行全部测试（无 Key 的 Provider 自动跳过）
.venv\Scripts\python.exe -m pytest tests/test_integration.py -v

# 只运行本地测试（无需网络，秒级完成）
.venv\Scripts\python.exe -m pytest tests/test_integration.py::TestConfigLoading tests/test_integration.py::TestMessageRouting tests/test_integration.py::TestClaudeRouter -v

# 运行真实 LLM 调用测试
.venv\Scripts\python.exe -m pytest tests/test_integration.py::TestLLMCalls -v -s
```

### 测试分层说明

| 层级 | 测试类 | 需要网络 | 覆盖内容 |
|------|--------|----------|----------|
| 第1层 | `TestConfigLoading` (16个) | ❌ | YAML 加载、字段校验、插值、Provider/Agent 完整性 |
| 第2层 | `TestMessageRouting` (13个) | ❌ | @mention 解析、历史管理、消息构建 |
| 第3层 | `TestClaudeRouter` (15个) | ❌ | `CliApiRouter` 双轨策略、mode 别名、legacy 兼容 |
| 第4层 | `TestLLMCalls` (7个) | ✅ | 真实 API 调用，无 Key 自动跳过 |
| 第5层 | `TestEndToEnd` (5个) | ✅ | 完整消息流、并发会议、错误隔离 |
| 第6层 | `TestProviderSwitching` (5个) | ❌ | YAML 配置合法性、mode 值校验 |

---

## 11. 故障排查

### `'str' object has no attribute 'choices'`

**原因**：`base_url` 缺少 `/v1` 路径后缀。  
**修复**：
```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1   ✅
DEEPSEEK_BASE_URL=https://api.deepseek.com      ❌
```

### `[provider] 不可用`

**dual_track Provider 报此错时**，两条路径均不可用：
- API 轨道：在 `.env` 中填写对应 `API_KEY`
- CLI 轨道：安装对应 CLI 工具并确保在 `PATH` 中可找到

**Claude 具体步骤**：
```powershell
# API Key 方式
CLAUDE_API_KEY=sk-ant-xxxx  # 在 .env 中填写

# CLI 方式
npm install -g @anthropic-ai/claude-code
claude login
```

### 某个 Agent 一直返回 `⚠️ 响应失败`

1. 运行 `check_config.py` 查看 Provider 状态
2. 确认 `.env` 中对应 Key 已填写
3. 检查 `llm_providers.yaml` 中 `base_url` 格式
4. 查看后端日志中的 `[Gateway]` 输出

### 路由总是去找 `architect`（兜底路由）

**原因**：主持人 LLM（DeepSeek）调用失败。  
```powershell
.venv\Scripts\python.exe -c "from config import LLM_PROVIDERS; print(LLM_PROVIDERS['deepseek'])"
```
确认 `api_key` 不为空，`base_url` 含 `/v1`。

### 添加新 Provider 后 `check_config.py` 报错

确认 `mode` 字段是合法值之一：  
`openai_compat` / `dual_track` / `cli_only` / `anthropic_api` / `claude_auto` / `claude_cli`

---

## 12. 变更日志

### v3 — 2026-04-20（当前版本）

#### 🔄 双轨路由通用化（`gateway.py`）
- **`ClaudeRouter` → `CliApiRouter`**：Claude 专属路由器升级为通用双轨路由器
  - 任何 Provider 配置 `mode: dual_track` 即可获得 API ↔ CLI 互备能力
  - `ClaudeRouter` 保留为向下兼容别名（`ClaudeRouter = CliApiRouter`）
- 新增 `cli_output_format` 字段：`stream_json`（Claude CLI）/ `plain`（其他 CLI 工具）
- 路由器缓存从 `_claude_router` 单例改为 `_routers[provider]` 字典，支持多个双轨 Provider 并存
- `_call_llm()` 按 `mode` 字段判断路由路径，不再硬编码 `provider == "claude"`

#### 📋 mode 体系重建（`llm_providers.yaml`）
- 新增 `dual_track` mode（推荐，取代 `claude_auto`）
- 新增 `cli_only` mode（纯 CLI，无需 API Key）
- 旧 mode 别名（`claude_auto` / `claude_cli` / `anthropic_api`）全部保留，向下兼容
- Claude 配置从 `mode: claude_auto` 迁移至 `mode: dual_track`
- 新增 Gemini CLI 双轨示例（注释状态）

#### ⚡ Token 优化（`gateway.py`）
- 路由调用改用精简 `_ROUTING_SYSTEM`（15字）+ `_ROUTING_RULES` 规则表，不再发完整 moderator system_prompt
- 路由 `max_tokens`: 300 → 80，温度: 0.3 → 0.1
- 历史管理改为 `_trim_history_by_tokens()`，按 Token 预算裁剪（替代按条数截取）
- `generate_moderator_summary()` 专家回复截断到 400 字，`max_tokens`: 400 → 200，移除历史上下文
- `update_history()` 存储截断版（用户≤500字，回复≤600字）
- 历史条数上限: 40 → 60（配合 Token 预算裁剪，条数限制仅作兜底）

#### 🖥️ 前端侧边栏可视化（`Sidebar.tsx` / `useWebSocket.ts`）
- 侧边栏每个 Agent 卡片新增：Provider 徽标（DS/CL/CX/GM）、发言计数、最后发言预览、时间戳
- 实时状态指示圈：🟡思考中（typing）/ 🔵首字节前（stream_start）/ 🟢已发言 / ⚫未参与
- `useWebSocket` 新增 `agentStats` 状态，追踪每个 Agent 的 messageCount / lastContent / isTyping / isStreaming
- 后端 welcome 事件补充 `provider` 字段，前端可展示每个 Agent 使用的模型服务

#### 🐛 Bug 修复（`gateway.py` / `.env`）
- 修复流式输出中 `chunk.choices` 为空列表时的 `IndexError`（ClaudeRouter 和 OpenAI-compat 两处）
- 修复 `.env` 中 `DEEPSEEK_BASE_URL` 缺少 `/v1` 导致的 `'str' object has no attribute 'choices'`
- 修复流式回调为普通函数（非 async）时的 `TypeError: object NoneType can't be used in 'await'`

#### 🧪 测试更新（`tests/test_integration.py`）
- `TestClaudeRouter` 全面重写为基于 `CliApiRouter` 的通用测试
- 新增测试：`cli_only` mode、旧 mode 别名兼容、`ClaudeRouter` 别名验证
- `test_provider_mode_values` 新增 `dual_track` / `cli_only` 到合法 mode 集合
- `test_history_limit_40` 更新上限为 60

---

### v4 — 2026-04-20

#### 🗂️ 后端模块化拆分（`gateway.py` 592行 → 4个文件）

| 文件 | 职责 |
|------|------|
| `core/token_utils.py` | Token 估算、按预算裁剪历史、文本截断 |
| `core/routing.py` | `@mention` 解析、路由规则常量（`ROUTING_SYSTEM` / `ROUTING_RULES`） |
| `providers/cli_api_router.py` | `CliApiRouter` 双轨路由器完整实现 |
| `gateway.py` | `IntelligentGateway` 纯编排层（~230行） |

- 所有旧导入（`_parse_mentions` / `CliApiRouter` / `ClaudeRouter`）在 `gateway.py` 保留向下兼容导出，测试无需修改
- 57 passed, 5 skipped，测试全部通过

#### 🖥️ 前端组件化拆分（`App.tsx` 297行 → 5个文件）

| 文件 | 职责 |
|------|------|
| `components/Header.tsx` | 顶栏（连接状态、用户名编辑、清空按钮） |
| `components/ChatInput.tsx` | 输入区（`@mention` 弹层、发送按钮） |
| `components/EmptyState.tsx` | 空状态欢迎页 + 快速问题卡片 |
| `hooks/useMention.ts` | `@mention` 检测、过滤、选择完整逻辑 |
| `App.tsx` | 纯编排层，只负责组合组件和状态传递（~115行） |

#### 🚀 启动脚本重构（无挂起窗口）

- `start_all.ps1`：改为后台静默启动（`-WindowStyle Hidden`），脚本执行完自动退出
  - 日志重定向至 `logs/backend.log` / `logs/frontend.log`
  - PID 写入 `logs/pids.txt`，供 `stop_all.ps1` 精确停止
- 新增 `stop_all.ps1`：按 PID 文件停止 + 兜底按端口（8765/5173/5174）清理
- `.gitignore` 新增 `logs/` 排除日志目录

---

### v2 — 2026-04-20

#### 配置系统
- `config.py` 重构为纯 YAML 加载器，移除所有硬编码 Provider/模型配置
- 新建 `llm_providers.yaml`：所有 Provider 定义集中管理
- 新建 `agents.yaml`：所有 Agent 定义，含 provider 绑定、skill 增强
- 支持 `${ENV_VAR}` 插值语法

#### 多 Provider 路由
- `gateway.py` 重构：per-agent LLM 路由，按 `llm_provider` 字段分派
- 新增 `ClaudeRouter`（现已升级为 `CliApiRouter`）
- 新增 `check_config.py` 配置验证脚本

#### 集成测试
- 新建 `tests/test_integration.py`，6层测试结构
- `pytest.ini` 配置 asyncio 模式

---

### v1 — 初始版本

- 多 Agent 会议聊天系统基础框架
- FastAPI WebSocket 后端，React + Vite 前端
- DeepSeek 单 Provider 支持
- `@mention` 路由、流式输出、打字指示器
