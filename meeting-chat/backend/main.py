"""
FastAPI WebSocket 服务端 - 会议聊天主服务
"""
import asyncio
import json
import time
import uuid
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import AGENTS, Message
from core.status_watcher import StatusWatcher
from core.workflow_runner import VALID_SKILLS, WorkflowRunner
from gateway import IntelligentGateway

app = FastAPI(title="多Agent会议聊天系统")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 网关单例
gateway = IntelligentGateway()

# WebSocket 连接管理
class ConnectionManager:
    def __init__(self):
        # meeting_id → list of websockets
        self.meetings: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, meeting_id: str):
        await ws.accept()
        self.meetings.setdefault(meeting_id, []).append(ws)

    def disconnect(self, ws: WebSocket, meeting_id: str):
        conns = self.meetings.get(meeting_id, [])
        if ws in conns:
            conns.remove(ws)

    async def broadcast(self, meeting_id: str, data: dict):
        for ws in list(self.meetings.get(meeting_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def send_personal(self, ws: WebSocket, data: dict):
        try:
            await ws.send_json(data)
        except Exception:
            pass

manager = ConnectionManager()


def make_event(event_type: str, data: dict) -> dict:
    return {"type": event_type, "data": data, "ts": time.time()}


async def process_user_message(meeting_id: str, user_content: str):
    """完整的消息处理流程：路由 → Agent响应 → 主持人总结"""
    
    # 1. 广播"主持人正在路由"状态
    await manager.broadcast(meeting_id, make_event("typing", {
        "agent_id": "moderator",
        "agent_name": AGENTS["moderator"]["name"],
        "agent_avatar": AGENTS["moderator"]["avatar"],
    }))
    
    # 2. 主持人路由决策
    route = await gateway.route_message(user_content, meeting_id)
    
    # 3. 广播路由决策消息
    route_msg = gateway.build_message(
        sender_id="moderator",
        content=f"📍 {route.summary}\n→ 转发给：{'、'.join(AGENTS[aid]['name'] for aid in route.route_to if aid in AGENTS)}",
        meeting_id=meeting_id,
        message_type="route_decision",
        target_agents=route.route_to,
    )
    await manager.broadcast(meeting_id, make_event("message", route_msg.model_dump()))

    # 4. 并发调用目标Agent生成流式回复
    agent_responses: dict[str, str] = {}
    
    async def handle_agent(agent_id: str):
        agent = AGENTS.get(agent_id)
        if not agent:
            return
        
        # 广播"Agent正在输入"
        await manager.broadcast(meeting_id, make_event("typing", {
            "agent_id": agent_id,
            "agent_name": agent["name"],
            "agent_avatar": agent["avatar"],
        }))
        
        # 流式消息ID（用于前端拼接）
        stream_msg_id = str(uuid.uuid4())
        
        # 先发送消息头
        await manager.broadcast(meeting_id, make_event("stream_start", {
            "msg_id": stream_msg_id,
            "agent_id": agent_id,
            "agent_name": agent["name"],
            "agent_avatar": agent["avatar"],
            "agent_color": agent["color"],
            "meeting_id": meeting_id,
        }))
        
        collected = []
        
        async def on_chunk(chunk: str):
            collected.append(chunk)
            await manager.broadcast(meeting_id, make_event("stream_chunk", {
                "msg_id": stream_msg_id,
                "chunk": chunk,
            }))
        
        full_response = await gateway.generate_agent_response(
            agent_id=agent_id,
            user_message=user_content,
            meeting_id=meeting_id,
            stream_callback=on_chunk,
        )
        
        # 发送流结束
        await manager.broadcast(meeting_id, make_event("stream_end", {
            "msg_id": stream_msg_id,
            "agent_id": agent_id,
            "full_content": full_response,
            "meeting_id": meeting_id,
        }))
        
        agent_responses[agent_id] = full_response
        # 线程安全地写入历史（await 版本）
        await gateway.update_history(meeting_id, user_content, agent_id, full_response)

    # 并发执行所有目标 Agent，单个失败不影响其他
    await asyncio.gather(*[handle_agent(aid) for aid in route.route_to], return_exceptions=True)

    # 5. 若有多个Agent回复，主持人做总结
    if len(agent_responses) > 1:
        await manager.broadcast(meeting_id, make_event("typing", {
            "agent_id": "moderator",
            "agent_name": AGENTS["moderator"]["name"],
            "agent_avatar": AGENTS["moderator"]["avatar"],
        }))
        
        summary_msg_id = str(uuid.uuid4())
        await manager.broadcast(meeting_id, make_event("stream_start", {
            "msg_id": summary_msg_id,
            "agent_id": "moderator",
            "agent_name": AGENTS["moderator"]["name"],
            "agent_avatar": AGENTS["moderator"]["avatar"],
            "agent_color": AGENTS["moderator"]["color"],
            "meeting_id": meeting_id,
        }))
        
        async def on_summary_chunk(chunk: str):
            await manager.broadcast(meeting_id, make_event("stream_chunk", {
                "msg_id": summary_msg_id,
                "chunk": chunk,
            }))
        
        summary = await gateway.generate_moderator_summary(
            user_content, agent_responses, meeting_id,
            stream_callback=on_summary_chunk
        )
        
        await manager.broadcast(meeting_id, make_event("stream_end", {
            "msg_id": summary_msg_id,
            "agent_id": "moderator",
            "full_content": summary,
            "meeting_id": meeting_id,
        }))

    # 6. 广播"处理完成"
    await manager.broadcast(meeting_id, make_event("done", {"meeting_id": meeting_id}))


@app.websocket("/ws/{meeting_id}")
async def websocket_endpoint(ws: WebSocket, meeting_id: str):
    await manager.connect(ws, meeting_id)
    
    # 发送欢迎消息
    welcome = make_event("system", {
        "content": f"🎉 欢迎加入会议室 [{meeting_id}]！本次会议由AI主持人协调，请开始发言。",
        "agents": [
            {
                "id": a["id"],
                "name": a["name"],
                "avatar": a["avatar"],
                "color": a["color"],
                "role": a["role"],
                "provider": a.get("llm_provider", "unknown"),
            }
            for a in AGENTS.values()
        ]
    })
    await manager.send_personal(ws, welcome)
    
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            
            if data.get("type") == "user_message":
                user_content = data.get("content", "").strip()
                if not user_content:
                    continue
                
                # 广播用户消息
                user_msg = {
                    "id": str(uuid.uuid4()),
                    "sender_id": "user",
                    "sender_name": data.get("user_name", "我"),
                    "sender_avatar": "👤",
                    "sender_color": "#64748b",
                    "content": user_content,
                    "timestamp": time.time(),
                    "message_type": "user",
                    "meeting_id": meeting_id,
                }
                await manager.broadcast(meeting_id, make_event("message", user_msg))
                
                # 异步处理（不阻塞WebSocket接收）
                asyncio.create_task(process_user_message(meeting_id, user_content))
            
            elif data.get("type") == "clear_history":
                gateway.clear_history(meeting_id)
                await manager.broadcast(meeting_id, make_event("system", {
                    "content": "🔄 会议记录已清空，可以开始新的议题。"
                }))

            elif data.get("type") == "workflow_command":
                # 主持人通过 WS 下发工作流控制命令
                skill   = data.get("skill", "*")
                command = data.get("command", "")
                if skill and command:
                    inbox.push(skill, command)
                    # 广播确认
                    cmd_short = command.split(":")[0]
                    label_map = {
                        "pause":    ("⏸️", "暂停工作流"),
                        "resume":   ("▶️", "恢复工作流"),
                        "abort":    ("⛔", "终止当前执行"),
                        "inject":   ("💉", "注入新指令"),
                        "set_task": ("🔀", "替换任务"),
                    }
                    icon, label = label_map.get(cmd_short, ("📌", command))
                    detail = command.split(":", 1)[1] if ":" in command else ""
                    moderator = AGENTS.get("moderator", {})
                    event_msg = {
                        "id":              str(uuid.uuid4()),
                        "sender_id":       "moderator",
                        "sender_name":     moderator.get("name", "主持人"),
                        "sender_avatar":   moderator.get("avatar", "🎙️"),
                        "sender_color":    moderator.get("color", "#6366f1"),
                        "content":         f"{icon} {label}：【{skill}】" + (f"\n{detail}" if detail else ""),
                        "timestamp":       time.time(),
                        "message_type":    "workflow_event",
                        "meeting_id":      meeting_id,
                        "workflow_from":   "moderator",
                        "workflow_to":     skill,
                        "workflow_status": "command",
                        "workflow_task":   command,
                    }
                    await manager.broadcast(meeting_id, make_event("workflow_event", event_msg))
    
    except WebSocketDisconnect:
        manager.disconnect(ws, meeting_id)


@app.get("/api/agents")
def get_agents():
    return list(AGENTS.values())


@app.get("/api/health")
def health():
    return {"status": "ok", "model": AGENTS["moderator"]["name"]}


# ── 工作流通知接口 ─────────────────────────────────────────────

# skill 名称 → 对应 Agent 信息（用于显示头像颜色）
_SKILL_TO_AGENT: dict[str, str] = {
    "chief_architect": "architect",
    "technical_lead":  "tech_lead",
    "dev_backend":     "backend",
    "dev_frontend":    "frontend",
    "meeting_chat":    "moderator",
}

_STATUS_LABEL = {
    "success": "✅ 完成",
    "failed":  "❌ 失败",
    "blocked": "⚠️ 阻塞",
    "patched": "🔧 已打补丁",
}


class WorkflowNotifyRequest(BaseModel):
    from_skill: str                          # 发起方 skill（如 chief_architect）
    to_skill: Optional[str] = None           # 接收方 skill（如 technical_lead）
    status: str = "success"                  # success | failed | blocked | patched
    task: Optional[str] = None               # 任务描述摘要
    detail: Optional[str] = None             # 可选详情
    meeting_id: Optional[str] = None         # 指定会议室，None 则广播到所有


@app.post("/api/workflow/notify")
async def workflow_notify(req: WorkflowNotifyRequest):
    """
    供 workflow.py 调用的工作流事件通知接口。
    收到通知后，向对应会议室广播 workflow_event 消息。
    """
    from_agent_id = _SKILL_TO_AGENT.get(req.from_skill, "moderator")
    to_agent_id   = _SKILL_TO_AGENT.get(req.to_skill, "") if req.to_skill else None
    from_agent    = AGENTS.get(from_agent_id, {})
    to_agent      = AGENTS.get(to_agent_id, {}) if to_agent_id else {}

    status_label  = _STATUS_LABEL.get(req.status, req.status)
    from_name     = from_agent.get("name", req.from_skill)
    to_name       = to_agent.get("name", req.to_skill) if to_agent else None

    # 构造消息体（与 ChatMessage 结构对齐）
    content_parts = [f"{status_label}"]
    if req.task:
        content_parts.append(f"任务：{req.task}")
    if req.detail:
        content_parts.append(req.detail)
    if to_name:
        content_parts.append(f"→ 下发给【{to_name}】")

    event_msg = {
        "id":              str(uuid.uuid4()),
        "sender_id":       from_agent_id,
        "sender_name":     from_name,
        "sender_avatar":   from_agent.get("avatar", "⚙️"),
        "sender_color":    from_agent.get("color", "#6366f1"),
        "content":         "\n".join(content_parts),
        "timestamp":       time.time(),
        "message_type":    "workflow_event",
        "meeting_id":      req.meeting_id or "all",
        # workflow 专属字段
        "workflow_from":   req.from_skill,
        "workflow_to":     req.to_skill,
        "workflow_status": req.status,
        "workflow_task":   req.task,
    }

    payload = make_event("workflow_event", event_msg)

    # 广播：指定会议室 or 所有活跃会议室
    if req.meeting_id and req.meeting_id in manager.meetings:
        await manager.broadcast(req.meeting_id, payload)
    else:
        for mid in list(manager.meetings.keys()):
            await manager.broadcast(mid, payload)

    return {"ok": True, "notified_rooms": list(manager.meetings.keys())}


# ── 工作流指令收件箱 ───────────────────────────────────────────
#
# workflow.py 每轮开始前 GET /api/workflow/inbox?skill=xxx 轮询
# 主持人在前端通过 WebSocket 发 workflow_command 消息写入收件箱
#
# 支持的命令：
#   pause              - 暂停工作流（workflow 阻塞等待 resume）
#   resume             - 恢复工作流
#   abort              - 终止当前 skill 执行
#   inject:<指令文本>  - 向当前 skill.md DYNAMIC 区域注入新指令
#   set_task:<新任务>  - 替换当前任务描述

from collections import deque

class WorkflowInbox:
    """线程安全的每 skill 指令队列"""
    def __init__(self):
        self._queues: dict[str, deque] = {}   # skill → 待消费命令队列
        self._paused: set[str] = set()        # 当前被 pause 的 skill

    def push(self, skill: str, command: str):
        self._queues.setdefault(skill, deque()).append({
            "command": command,
            "ts": time.time(),
        })
        if command == "pause":
            self._paused.add(skill)
        elif command == "resume":
            self._paused.discard(skill)

    def pop_all(self, skill: str) -> list[dict]:
        """取出并清空该 skill 的所有待处理命令"""
        q = self._queues.pop(skill, deque())
        return list(q)

    def is_paused(self, skill: str) -> bool:
        return skill in self._paused

    def status(self) -> dict:
        return {
            "paused": list(self._paused),
            "pending": {k: len(v) for k, v in self._queues.items() if v},
        }

inbox = WorkflowInbox()


class WorkflowCommandRequest(BaseModel):
    skill: str          # 目标 skill（如 chief_architect / * 表示全部）
    command: str        # pause | resume | abort | inject:<text> | set_task:<text>
    meeting_id: Optional[str] = None


@app.post("/api/workflow/command")
async def workflow_command(req: WorkflowCommandRequest):
    """
    主持人下发控制命令。
    前端通过 WebSocket 发 workflow_command 事件 → 转写到此接口。
    workflow.py 每轮轮询 /api/workflow/inbox 消费命令。
    """
    targets = list(inbox._queues.keys()) + list(inbox._paused) if req.skill == "*" else [req.skill]
    # 如果指定 skill 不在任何队列，仍要创建命令
    if req.skill != "*":
        targets = [req.skill]

    for skill in targets:
        inbox.push(skill, req.command)

    # 广播命令确认消息到 meeting-chat
    cmd_short = req.command.split(":")[0]
    label_map = {
        "pause":    ("⏸️", "暂停工作流"),
        "resume":   ("▶️", "恢复工作流"),
        "abort":    ("⛔", "终止当前执行"),
        "inject":   ("💉", "注入新指令"),
        "set_task": ("🔀", "替换任务"),
    }
    icon, label = label_map.get(cmd_short, ("📌", req.command))
    detail = req.command.split(":", 1)[1] if ":" in req.command else ""

    event_msg = {
        "id":              str(uuid.uuid4()),
        "sender_id":       "moderator",
        "sender_name":     AGENTS.get("moderator", {}).get("name", "主持人"),
        "sender_avatar":   AGENTS.get("moderator", {}).get("avatar", "🎙️"),
        "sender_color":    AGENTS.get("moderator", {}).get("color", "#6366f1"),
        "content":         f"{icon} {label}：【{req.skill}】" + (f"\n{detail}" if detail else ""),
        "timestamp":       time.time(),
        "message_type":    "workflow_event",
        "meeting_id":      req.meeting_id or "all",
        "workflow_from":   "moderator",
        "workflow_to":     req.skill,
        "workflow_status": "command",
        "workflow_task":   req.command,
    }
    payload = make_event("workflow_event", event_msg)
    if req.meeting_id and req.meeting_id in manager.meetings:
        await manager.broadcast(req.meeting_id, payload)
    else:
        for mid in list(manager.meetings.keys()):
            await manager.broadcast(mid, payload)

    return {"ok": True, "targets": targets, "inbox": inbox.status()}


@app.get("/api/workflow/inbox")
def workflow_inbox(skill: str):
    """
    workflow.py 每轮开始前轮询此接口，获取主持人下发的命令。
    返回命令列表并清空队列。同时返回 is_paused 状态。
    """
    commands = inbox.pop_all(skill)
    return {
        "skill":     skill,
        "is_paused": inbox.is_paused(skill),
        "commands":  commands,
    }


@app.get("/api/workflow/status")
def workflow_inbox_status():
    """返回当前收件箱整体状态（供前端展示）"""
    return inbox.status()


# ── 工作流子进程触发 + status.json 文件监听 ───────────────────

async def _on_workflow_line(line: str, task_id: str, skill: Optional[str], meeting_id: str):
    """子进程 stdout 逐行回调：广播为 workflow_log 事件。"""
    payload = make_event("workflow_log", {
        "line":    line,
        "task_id": task_id,
        "skill":   skill,
    })
    if meeting_id and meeting_id in manager.meetings:
        await manager.broadcast(meeting_id, payload)
    else:
        for mid in list(manager.meetings.keys()):
            await manager.broadcast(mid, payload)


async def _on_workflow_exit(
    returncode: int,
    task_id: str,
    mode: str,
    skill: Optional[str],
    task_desc: str,
    meeting_id: str,
):
    """子进程退出回调：广播一条 workflow_event 总结消息。"""
    ok = returncode == 0
    from_skill = skill or "meeting_chat"
    from_agent_id = _SKILL_TO_AGENT.get(from_skill, "moderator")
    from_agent = AGENTS.get(from_agent_id, {})
    label = "✅ 工作流完成" if ok else f"❌ 工作流失败 (returncode={returncode})"
    scope = f"全链路 · {task_desc}" if mode == "all" else f"{skill} · {task_desc}"

    event_msg = {
        "id":              str(uuid.uuid4()),
        "sender_id":       from_agent_id,
        "sender_name":     from_agent.get("name", from_skill),
        "sender_avatar":   from_agent.get("avatar", "⚙️"),
        "sender_color":    from_agent.get("color", "#6366f1"),
        "content":         f"{label}\n{scope}\n任务 ID: {task_id}",
        "timestamp":       time.time(),
        "message_type":    "workflow_event",
        "meeting_id":      meeting_id or "all",
        "workflow_from":   from_skill,
        "workflow_to":     None,
        "workflow_status": "success" if ok else "failed",
        "workflow_task":   task_desc,
    }
    payload = make_event("workflow_event", event_msg)
    if meeting_id and meeting_id in manager.meetings:
        await manager.broadcast(meeting_id, payload)
    else:
        for mid in list(manager.meetings.keys()):
            await manager.broadcast(mid, payload)


async def _broadcast_status(snapshot: dict):
    """status.json 变化回调：向所有活跃会议室广播 workflow_status 事件。"""
    payload = make_event("workflow_status", snapshot)
    for mid in list(manager.meetings.keys()):
        await manager.broadcast(mid, payload)


runner = WorkflowRunner(on_line=_on_workflow_line, on_exit=_on_workflow_exit)
watcher: Optional[StatusWatcher] = None


@app.on_event("startup")
async def _startup():
    global watcher
    loop = asyncio.get_running_loop()
    watcher = StatusWatcher(loop=loop, on_change=_broadcast_status)
    watcher.start()


@app.on_event("shutdown")
async def _shutdown():
    global watcher
    if watcher is not None:
        watcher.stop()
        watcher = None
    runner.terminate_if_running()


class WorkflowRunRequest(BaseModel):
    mode: Literal["all", "skill"]
    task: str
    meeting_id: str
    skill: Optional[str] = None


@app.post("/api/workflow/run")
async def workflow_run(req: WorkflowRunRequest):
    """
    触发 .claude 工作流子进程。
    - mode=all   → optimize_all.py（全链路）
    - mode=skill → workflow.py TARGET_SKILL=<skill>（单技能）
    已有任务在跑 → 返回 409。
    """
    result = runner.start(
        mode=req.mode,
        task=req.task,
        meeting_id=req.meeting_id,
        skill=req.skill,
    )
    if not result.get("ok"):
        reason = result.get("reason", "unknown")
        if reason == "busy":
            raise HTTPException(status_code=409, detail=result)
        raise HTTPException(status_code=400, detail=result)

    # 立即广播一条 workflow_event 告知会议室：任务已启动
    from_skill = req.skill or "meeting_chat"
    from_agent_id = _SKILL_TO_AGENT.get(from_skill, "moderator")
    from_agent = AGENTS.get(from_agent_id, {})
    scope = "全链路" if req.mode == "all" else (req.skill or "")
    event_msg = {
        "id":              str(uuid.uuid4()),
        "sender_id":       from_agent_id,
        "sender_name":     from_agent.get("name", from_skill),
        "sender_avatar":   from_agent.get("avatar", "⚙️"),
        "sender_color":    from_agent.get("color", "#6366f1"),
        "content":         f"⏵ 已触发工作流：{scope}\n任务：{req.task}\n任务 ID: {result['task_id']}",
        "timestamp":       time.time(),
        "message_type":    "workflow_event",
        "meeting_id":      req.meeting_id,
        "workflow_from":   from_skill,
        "workflow_to":     None,
        "workflow_status": "command",
        "workflow_task":   req.task,
    }
    await manager.broadcast(req.meeting_id, make_event("workflow_event", event_msg))

    return result


@app.get("/api/workflow/run/current")
def workflow_run_current():
    """查询当前正在跑的工作流（供前端恢复刷新后的状态）。"""
    return {"running": runner.is_running(), "current": runner.current()}


@app.get("/api/workflow/snapshot")
def workflow_snapshot():
    """返回 status.json 首次加载快照（WebSocket 尚未推送前使用）。"""
    snap = StatusWatcher.read_once()
    if snap is None:
        return {"ok": False, "reason": "status_missing"}
    return {"ok": True, "snapshot": snap}


# 挂载前端静态文件
import os
static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
else:
    @app.get("/")
    def root():
        return {"message": "请先构建前端：cd frontend && npm run build"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=True)
