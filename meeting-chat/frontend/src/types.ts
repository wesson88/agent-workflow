// 消息类型定义
export interface AgentInfo {
  id: string
  name: string
  avatar: string
  color: string
  role: 'moderator' | 'expert'
  provider?: string          // llm_provider 名称，如 deepseek / claude / gemini
}

export interface AgentStat {
  messageCount: number       // 该 Agent 发言次数
  lastContent: string        // 最后一条消息摘要
  lastTime: number           // 最后发言时间戳
  isTyping: boolean          // 当前是否正在输出
  isStreaming: boolean       // 当前是否流式输出中
}

export interface ChatMessage {
  id: string
  sender_id: string
  sender_name: string
  sender_avatar: string
  sender_color: string
  content: string
  timestamp: number
  message_type: 'user' | 'agent' | 'system' | 'route_decision' | 'workflow_event'
  target_agents?: string[]
  meeting_id: string
  streaming?: boolean
  // workflow_event 专属字段
  workflow_from?: string      // 发起方 skill 名
  workflow_to?: string        // 接收方 skill 名
  workflow_status?: 'success' | 'failed' | 'blocked' | 'patched' | 'command'
  workflow_task?: string      // 任务描述摘要
}

export interface TypingInfo {
  agent_id: string
  agent_name: string
  agent_avatar: string
}

// ── 工作流 status.json 快照（.claude/status.json schema 1.1） ──
export interface SkillRegistryEntry {
  status: 'idle' | 'busy' | 'success' | 'failed' | 'blocked' | 'monitoring'
  version?: string
  last_run?: string | null
  last_patch_timestamp?: string | null
  error_count?: number
  consecutive_failures?: number
  last_output_path?: string | null
}

export interface StatusJsonSnapshot {
  schema_version?: string
  project_name?: string
  system_state?: 'normal' | 'blocked'
  block_reason?: string | null
  recursion_config?: {
    max_depth?: number
    current_depth?: number
    halt_on_failure?: boolean
    consecutive_failures_limit?: number
  }
  skill_registry: Record<string, SkillRegistryEntry>
  active_tasks?: Array<{
    task_id: string
    assignee: string
    description?: string
    status?: string
    priority?: string
    created_at?: string
    dependencies?: string[]
    outputs?: string[]
    retry_count?: number
  }>
}

export interface WorkflowLogLine {
  line: string
  task_id: string
  skill?: string | null
}

export type WsEvent =
  | { type: 'system'; data: { content: string; agents?: AgentInfo[] } }
  | { type: 'workflow_event'; data: ChatMessage }
  | { type: 'workflow_status'; data: StatusJsonSnapshot }
  | { type: 'workflow_log'; data: WorkflowLogLine }
  | { type: 'message'; data: ChatMessage }
  | { type: 'typing'; data: TypingInfo }
  | { type: 'stream_start'; data: { msg_id: string; agent_id: string; agent_name: string; agent_avatar: string; agent_color: string; meeting_id: string } }
  | { type: 'stream_chunk'; data: { msg_id: string; chunk: string } }
  | { type: 'stream_end'; data: { msg_id: string; agent_id: string; full_content: string; meeting_id: string } }
  | { type: 'done'; data: { meeting_id: string } }
