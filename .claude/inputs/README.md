# 脑暴素材目录（Inputs）

本目录存放 `product_manager` skill 生成 PRD 所需的**原材料**。
product_manager 在运行时会扫描本目录下所有 `.md` 文件，综合后产出 `../requirements/PRD.md`。

## 用法

把任何和需求相关的 markdown 文件放进来即可，命名随意。
product_manager 会：
1. 读取目录下全部 `*.md`（排除 `.example.*` 和 `README.md`）
2. 带文件名作为分隔上下文，一起喂给 Claude
3. 生成的 PRD 末尾会列出「参考资料」章节，用相对链接把本次引用的每份素材指回此目录，便于下游架构师/开发者回溯决策依据

## 推荐命名约定（可选）

使用统一前缀有助于 LLM 识别素材类型，也方便人工检索：

| 前缀 | 用途 | 示例 |
|------|------|------|
| `business_brief.md` | 核心业务简报（可直接复制模板） | `business_brief.md` |
| `brainstorm-*.md` | 脑暴产出（superpowers brainstorming / 其他模型） | `brainstorm-mvp-scope.md` |
| `meeting-*.md` | 会议纪要、用户访谈记录 | `meeting-2026-04-20.md` |
| `research-*.md` | 用户/市场调研 | `research-user-persona.md` |
| `competitor-*.md` | 竞品分析 | `competitor-trello.md` |
| `spec-*.md` | 其他工具产出的 specs/plans | `spec-from-chatgpt.md` |

## 优先级

`business_brief.md` 如果存在，会被 product_manager 置于上下文最前（视为事实基线），其余按文件名字典序。

## 与 superpowers brainstorming 配合

使用 Claude Code 的 `brainstorming` skill 得到的产出，直接保存为 `brainstorm-<topic>.md` 放入本目录即可；下一次运行 product_manager 就会综合进 PRD。

## 被忽略的文件

- 以 `.` 开头的隐藏文件
- 文件名包含 `.example.`（模板示例）
- `README.md`（就是本文件）
- 空文件
