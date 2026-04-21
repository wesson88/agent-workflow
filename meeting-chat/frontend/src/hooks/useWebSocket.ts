import { useEffect, useRef, useState, useCallback } from 'react'
import type { ChatMessage, AgentInfo, AgentStat, TypingInfo, WsEvent, StatusJsonSnapshot } from '../types'

const WS_URL = (meetingId: string) => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host =
    window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
      ? `${window.location.hostname}:8765`
      : window.location.host
  return `${proto}//${host}/ws/${meetingId}`
}

const MAX_RECONNECT_DELAY = 16000
const BASE_RECONNECT_DELAY = 1000

export function useWebSocket(meetingId: string) {
  const ws = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [agents, setAgents] = useState<AgentInfo[]>([])
  const [typingAgents, setTypingAgents] = useState<TypingInfo[]>([])
  const [processing, setProcessing] = useState(false)
  const [agentStats, setAgentStats] = useState<Record<string, AgentStat>>({})
  const [workflowStatus, setWorkflowStatus] = useState<StatusJsonSnapshot | null>(null)

  // 流式内容用 ref 维护，避免每个 chunk 触发全量 map 重渲染
  const streamingRef = useRef<Map<string, string>>(new Map())
  // 重连计时器
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectDelay = useRef(BASE_RECONNECT_DELAY)
  const unmounted = useRef(false)

  const addOrUpdate = useCallback((msg: ChatMessage) => {
    setMessages(prev => {
      const idx = prev.findIndex(m => m.id === msg.id)
      if (idx >= 0) {
        const next = [...prev]
        next[idx] = msg
        return next
      }
      return [...prev, msg]
    })
  }, [])

  const connect = useCallback(() => {
    if (unmounted.current) return
    const socket = new WebSocket(WS_URL(meetingId))
    ws.current = socket

    socket.onopen = () => {
      setConnected(true)
      reconnectDelay.current = BASE_RECONNECT_DELAY // 重置退避时间
    }

    socket.onclose = () => {
      setConnected(false)
      setProcessing(false)
      if (unmounted.current) return
      // 指数退避重连
      reconnectTimer.current = setTimeout(() => {
        reconnectDelay.current = Math.min(reconnectDelay.current * 2, MAX_RECONNECT_DELAY)
        connect()
      }, reconnectDelay.current)
    }

    socket.onerror = () => socket.close()

    socket.onmessage = (e) => {
      const event: WsEvent = JSON.parse(e.data)

      if (event.type === 'system') {
        if (event.data.agents) setAgents(event.data.agents)
        const sysMsg: ChatMessage = {
          id: `sys-${Date.now()}-${Math.random()}`,
          sender_id: 'system',
          sender_name: '系统',
          sender_avatar: '🔔',
          sender_color: '#64748b',
          content: event.data.content,
          timestamp: Date.now() / 1000,
          message_type: 'system',
          meeting_id: meetingId,
        }
        setMessages(prev => [...prev, sysMsg])
      }

      else if (event.type === 'workflow_event') {
        setMessages(prev => [...prev, event.data])
      }

      else if (event.type === 'workflow_status') {
        setWorkflowStatus(event.data)
      }

      else if (event.type === 'workflow_log') {
        const { line, task_id, skill } = event.data
        const tag = skill ? `[${skill}]` : `[workflow ${task_id.slice(0, 6)}]`
        const logMsg: ChatMessage = {
          id: `log-${task_id}-${Date.now()}-${Math.random()}`,
          sender_id: 'system',
          sender_name: 'workflow',
          sender_avatar: '🛠️',
          sender_color: '#64748b',
          content: `${tag} ${line}`,
          timestamp: Date.now() / 1000,
          message_type: 'system',
          meeting_id: meetingId,
        }
        setMessages(prev => [...prev, logMsg])
      }

      else if (event.type === 'message') {
        setTypingAgents(prev => prev.filter(t => t.agent_id !== event.data.sender_id))
        addOrUpdate(event.data)
        if (event.data.message_type !== 'user') setProcessing(true)
      }

      else if (event.type === 'typing') {
        setTypingAgents(prev => {
          if (prev.find(t => t.agent_id === event.data.agent_id)) return prev
          return [...prev, event.data]
        })
        setAgentStats(prev => ({
          ...prev,
          [event.data.agent_id]: {
            ...prev[event.data.agent_id],
            messageCount: prev[event.data.agent_id]?.messageCount ?? 0,
            lastContent: prev[event.data.agent_id]?.lastContent ?? '',
            lastTime: prev[event.data.agent_id]?.lastTime ?? 0,
            isTyping: true,
            isStreaming: false,
          }
        }))
      }

      else if (event.type === 'stream_start') {
        const { msg_id, agent_id, agent_name, agent_avatar, agent_color, meeting_id } = event.data
        streamingRef.current.set(msg_id, '')
        setTypingAgents(prev => prev.filter(t => t.agent_id !== agent_id))
        setAgentStats(prev => ({
          ...prev,
          [agent_id]: {
            ...prev[agent_id],
            messageCount: prev[agent_id]?.messageCount ?? 0,
            lastContent: prev[agent_id]?.lastContent ?? '',
            lastTime: Date.now() / 1000,
            isTyping: false,
            isStreaming: true,
          }
        }))
        const msg: ChatMessage = {
          id: msg_id,
          sender_id: agent_id,
          sender_name: agent_name,
          sender_avatar: agent_avatar,
          sender_color: agent_color,
          content: '',
          timestamp: Date.now() / 1000,
          message_type: 'agent',
          streaming: true,
          meeting_id,
        }
        addOrUpdate(msg)
      }

      else if (event.type === 'stream_chunk') {
        const { msg_id, chunk } = event.data
        const current = streamingRef.current.get(msg_id) ?? ''
        const next = current + chunk
        streamingRef.current.set(msg_id, next)
        // 只更新目标消息，不做全量 map
        setMessages(prev => {
          const idx = prev.findIndex(m => m.id === msg_id)
          if (idx < 0) return prev
          const next_msgs = [...prev]
          next_msgs[idx] = { ...next_msgs[idx], content: next, streaming: true }
          return next_msgs
        })
      }

      else if (event.type === 'stream_end') {
        const { msg_id, full_content, agent_id } = event.data
        streamingRef.current.delete(msg_id)
        setMessages(prev => {
          const idx = prev.findIndex(m => m.id === msg_id)
          if (idx < 0) return prev
          const next_msgs = [...prev]
          next_msgs[idx] = { ...next_msgs[idx], content: full_content, streaming: false }
          return next_msgs
        })
        setAgentStats(prev => ({
          ...prev,
          [agent_id]: {
            messageCount: (prev[agent_id]?.messageCount ?? 0) + 1,
            lastContent: full_content.replace(/\n/g, ' ').slice(0, 60),
            lastTime: Date.now() / 1000,
            isTyping: false,
            isStreaming: false,
          }
        }))
      }

      else if (event.type === 'done') {
        setTypingAgents([])
        setProcessing(false)
        // 清除所有 isTyping / isStreaming 状态
        setAgentStats(prev => {
          const next = { ...prev }
          Object.keys(next).forEach(k => {
            next[k] = { ...next[k], isTyping: false, isStreaming: false }
          })
          return next
        })
      }
    }
  }, [meetingId, addOrUpdate])

  useEffect(() => {
    unmounted.current = false
    connect()
    return () => {
      unmounted.current = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      ws.current?.close()
    }
  }, [connect])

  const sendMessage = useCallback((content: string, userName: string) => {
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return
    setProcessing(true)
    ws.current.send(JSON.stringify({ type: 'user_message', content, user_name: userName }))
  }, [])

  const clearHistory = useCallback(() => {
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return
    ws.current.send(JSON.stringify({ type: 'clear_history' }))
    setMessages([])
    setTypingAgents([])
    setProcessing(false)
  }, [])

  const sendWorkflowCommand = useCallback((skill: string, command: string) => {
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return
    ws.current.send(JSON.stringify({ type: 'workflow_command', skill, command }))
  }, [])

  return {
    connected,
    messages,
    agents,
    typingAgents,
    processing,
    agentStats,
    workflowStatus,
    sendMessage,
    clearHistory,
    sendWorkflowCommand,
  }
}
