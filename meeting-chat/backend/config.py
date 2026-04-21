"""
多Agent会议聊天系统 - 配置加载器
所有 Provider 和 Agent 配置均从 YAML 文件读取，代码不硬编码任何模型或 Key。

配置文件：
  llm_providers.yaml  - LLM Provider 定义（模型、接入模式、参数）
  agents.yaml         - Agent 定义（角色、Provider 绑定、Prompt）
"""
from __future__ import annotations
import os
import re
import yaml
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, Literal
from dotenv import load_dotenv

load_dotenv()

# ── 基础路径 ──────────────────────────────────────────────────
_BASE_DIR   = Path(__file__).parent
_SKILL_BASE = (_BASE_DIR / ".." / ".." / ".claude" / "skills").resolve()

# ── 环境变量插值：替换 ${VAR_NAME} ───────────────────────────
def _interpolate(value: str) -> str:
    """将 ${ENV_VAR} 替换为对应环境变量值，未设置时返回空字符串"""
    return re.sub(
        r'\$\{(\w+)\}',
        lambda m: os.getenv(m.group(1), ""),
        value,
    )

def _resolve(obj):
    """递归对 dict/list/str 做环境变量插值"""
    if isinstance(obj, dict):
        return {k: _resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(i) for i in obj]
    if isinstance(obj, str):
        return _interpolate(obj)
    return obj

# ── YAML 加载 ─────────────────────────────────────────────────
def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

_raw_providers = _load_yaml(_BASE_DIR / "llm_providers.yaml")
_raw_agents    = _load_yaml(_BASE_DIR / "agents.yaml")

# 插值环境变量
LLM_PROVIDERS: dict[str, dict] = _resolve(_raw_providers.get("providers", {}))
DEFAULT_PROVIDER = "deepseek"

# ── skill.md 加载（可选增强）──────────────────────────────────
def _load_skill_md(skill_id: str) -> str:
    path = _SKILL_BASE / skill_id / "skill.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    content = re.sub(r'^---.*?---\s*', '', content, flags=re.DOTALL)
    content = re.sub(r'<!--\s*DYNAMIC_START\s*-->.*?<!--\s*DYNAMIC_END\s*-->', '', content, flags=re.DOTALL)
    return content.strip()

# ── Agent 构建 ────────────────────────────────────────────────
def _build_agents(raw: dict) -> dict[str, dict]:
    agents = {}
    for agent_id, cfg in raw.items():
        base_prompt = cfg.get("prompt", "").strip()

        # skill.md 增强（按 Agent 粒度控制）
        if cfg.get("skill_augment") and cfg.get("skill_id"):
            skill_content = _load_skill_md(cfg["skill_id"])
            if skill_content:
                base_prompt += (
                    "\n\n---\n## 你在自动化工作流中的完整职责参考（仅供背景了解）\n\n"
                    + skill_content
                )

        agents[agent_id] = {
            "id":           agent_id,
            "name":         cfg.get("name", agent_id),
            "avatar":       cfg.get("avatar", "🤖"),
            "color":        cfg.get("color", "#888888"),
            "role":         cfg.get("role", "expert"),
            "llm_provider": cfg.get("provider", DEFAULT_PROVIDER),
            "system_prompt": base_prompt,
            # Agent 级别的参数覆盖（可选）
            "temperature":  cfg.get("temperature"),
            "max_tokens":   cfg.get("max_tokens"),
        }
    return agents

AGENTS: dict[str, dict] = _build_agents(_raw_agents)

# ── 消息模型 ──────────────────────────────────────────────────
class Message(BaseModel):
    id: str
    sender_id: str
    sender_name: str
    sender_avatar: str
    sender_color: str
    content: str
    timestamp: float
    message_type: Literal["user", "agent", "system", "route_decision"]
    target_agents: Optional[list[str]] = None
    meeting_id: str

class RouteDecision(BaseModel):
    route_to: list[str]
    broadcast: bool = False
    summary: str