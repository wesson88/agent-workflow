import { useState, useRef, useEffect, useCallback } from 'react'
import { useWebSocket } from './hooks/useWebSocket'
import { useMention } from './hooks/useMention'
import Header from './components/Header'
import ChatInput from './components/ChatInput'
import EmptyState from './components/EmptyState'
import MessageBubble from './components/MessageBubble'
import TypingIndicator from './components/TypingIndicator'
import Sidebar from './components/Sidebar'
import WorkflowPanel from './components/WorkflowPanel'

const MEETING_ID = 'meeting-' + (
  localStorage.getItem('meeting_id') || (() => {
    const id = Math.random().toString(36).slice(2, 8).toUpperCase()
    localStorage.setItem('meeting_id', id)
    return id
  })()
)

export default function App() {
  const [userName, setUserName] = useState(() => localStorage.getItem('user_name') || '我')
  const [editingName, setEditingName] = useState(false)
  const [tempName, setTempName] = useState(userName)
  const [input, setInput] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(true)

  const { connected, messages, agents, typingAgents, processing, agentStats, workflowStatus, sendMessage, clearHistory, sendWorkflowCommand } =
    useWebSocket(MEETING_ID)

  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const { mentionList, slashList, detectMention, pickMention, pickSlash, closeMention } = useMention(
    agents, input, setInput, textareaRef,
  )

  // 自动滚动（仅在接近底部时）
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, typingAgents])

  // 自动调整 textarea 高度
  const adjustHeight = useCallback(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [])

  const handleInputChange = useCallback((val: string) => {
    setInput(val)
    adjustHeight()
    detectMention(val)
  }, [adjustHeight, detectMention])

  const triggerWorkflow = useCallback(async (rawTarget: string, task: string) => {
    const target = rawTarget.toLowerCase()
    const payload: Record<string, unknown> = {
      task,
      meeting_id: MEETING_ID,
      mode: target === 'all' ? 'all' : 'skill',
    }
    if (target !== 'all') payload.skill = target
    try {
      const res = await fetch('/api/workflow/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (res.status === 409) {
        // 服务端会返回 {detail: {ok:false, reason:'busy', current: {...}}}
        // workflow_event 消息会从 WebSocket 回来，这里不额外处理
        const body = await res.json().catch(() => null)
        console.warn('workflow busy:', body)
      } else if (!res.ok) {
        console.error('workflow run failed:', res.status, await res.text().catch(() => ''))
      }
    } catch (err) {
      console.error('workflow run error:', err)
    }
  }, [])

  const handleSend = useCallback(() => {
    const content = input.trim()
    if (!content) return

    // 斜杠命令：/run <target> <task>。不受 processing 限制（REST 通道）。
    const slash = content.match(/^\/run\s+(\S+)\s+([\s\S]+)$/)
    if (slash) {
      const [, target, task] = slash
      triggerWorkflow(target, task.trim())
      setInput('')
      closeMention()
      if (textareaRef.current) textareaRef.current.style.height = 'auto'
      return
    }

    if (processing || !connected) return
    sendMessage(content, userName)
    setInput('')
    closeMention()
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }, [input, processing, connected, sendMessage, userName, closeMention, triggerWorkflow])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (mentionList.length > 0 && e.key === 'Escape') { closeMention(); return }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }, [handleSend, mentionList, closeMention])

  const saveName = () => {
    const n = tempName.trim() || '我'
    setUserName(n)
    localStorage.setItem('user_name', n)
    setEditingName(false)
  }

  return (
    <div className="flex h-screen bg-slate-950 overflow-hidden">
      {sidebarOpen && <Sidebar agents={agents} agentStats={agentStats} meetingId={MEETING_ID} />}

      <div className="flex-1 flex flex-col min-w-0">
        <Header
          meetingId={MEETING_ID}
          agentCount={agents.length}
          connected={connected}
          userName={userName}
          editingName={editingName}
          tempName={tempName}
          onToggleSidebar={() => setSidebarOpen(v => !v)}
          onEditName={() => { setTempName(userName); setEditingName(true) }}
          onSaveName={saveName}
          onCancelEdit={() => setEditingName(false)}
          onTempNameChange={setTempName}
          onClearHistory={clearHistory}
        />

        <main ref={containerRef} className="flex-1 overflow-y-auto py-4 space-y-1">
          {messages.length === 0 && (
            <EmptyState onSelect={q => { handleInputChange(q); textareaRef.current?.focus() }} />
          )}
          {messages.map(msg => (
            <MessageBubble key={msg.id} message={msg} isOwn={msg.message_type === 'user'} />
          ))}
          <TypingIndicator agents={typingAgents} />
          <div ref={messagesEndRef} />
        </main>

        <ChatInput
          input={input}
          connected={connected}
          processing={processing}
          mentionList={mentionList}
          slashList={slashList}
          textareaRef={textareaRef}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onSend={handleSend}
          onPickMention={agent => { pickMention(agent); adjustHeight() }}
          onPickSlash={t => { pickSlash(t); adjustHeight() }}
          onAtClick={() => handleInputChange(input + '@')}
        />

        <WorkflowPanel status={workflowStatus} onSendCommand={sendWorkflowCommand} />
      </div>
    </div>
  )
}
