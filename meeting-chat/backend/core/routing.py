"""
消息路由：@mention 解析 + 主持人路由规则
"""
import re
from config import AGENTS

# @mention 正则：匹配 @架构师 @backend 等
_MENTION_RE = re.compile(r'@([\w\u4e00-\u9fff]+)')

# 中文名 → agent_id 反查表
NAME_TO_ID: dict[str, str] = {
    v["name"]: k for k, v in AGENTS.items() if k != "moderator"
}

# 主持人路由专用精简 prompt（节省 ~60% token）
ROUTING_SYSTEM = (
    "你是会议主持人，只负责路由决策。"
    "输出严格JSON，不加任何解释：\n"
    '{"route_to":["agent_id"],"broadcast":false,"summary":"原因"}'
)

# 路由规则表（独立维护，不占 Agent system_prompt 空间）
ROUTING_RULES = """路由规则（按关键词选择 agent_id）：
architect: 架构/系统设计/技术选型/分布式
tech_lead: 任务拆解/排期/协调/实现计划
backend: 后端/API/数据库/服务端/Python/Go
frontend: 前端/React/Vue/组件/CSS/页面
ux: 交互/UX/原型/设计/UI生成/用户体验
product: 需求/产品/用户故事/功能规划/PRD
qa: 测试/质量/bug/自动化/性能测试
多人: 新功能完整讨论→product+ux+architect；完整链路→architect+tech_lead"""


def parse_mentions(text: str) -> list[str]:
    """从消息中解析 @mention，返回匹配到的 agent_id 列表"""
    hits = []
    for token in _MENTION_RE.findall(text):
        if token in AGENTS and token != "moderator":
            hits.append(token)
        elif token in NAME_TO_ID:
            hits.append(NAME_TO_ID[token])
    return list(dict.fromkeys(hits))
