import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, ArrowRight, AlertTriangle, CheckCircle, XCircle, Wrench } from 'lucide-react'
import type { ChatMessage } from '../types'

interface Props {
  message: ChatMessage
  isOwn?: boolean
}

function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** 将 @mention 渲染成高亮标签 */
function HighlightMentions({ text }: { text: string }) {
  const parts = text.split(/(@[\w\u4e00-\u9fff]+)/g)
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('@') ? (
          <span key={i} className="text-indigo-400 font-semibold bg-indigo-950/50 rounded px-1">
            {p}
          </span>
        ) : (
          <span key={i}>{p}</span>
        )
      )}
    </>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <button
      onClick={copy}
      className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded text-slate-500 hover:text-slate-300 hover:bg-slate-700"
      title="复制内容"
    >
      {copied ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
    </button>
  )
}

export default function MessageBubble({ message, isOwn }: Props) {
  const isUser = message.message_type === 'user'
  const isSystem = message.message_type === 'system'
  const isRoute = message.message_type === 'route_decision'
  const isWorkflow = message.message_type === 'workflow_event'

  if (isWorkflow) {
    const statusConfig = {
      success: { icon: <CheckCircle size={13} />, color: 'text-emerald-400', bg: 'bg-emerald-950/40 border-emerald-800/50', label: '完成' },
      failed:  { icon: <XCircle size={13} />,    color: 'text-red-400',     bg: 'bg-red-950/40 border-red-800/50',         label: '失败' },
      blocked: { icon: <AlertTriangle size={13} />, color: 'text-amber-400', bg: 'bg-amber-950/40 border-amber-800/50',    label: '阻塞' },
      patched: { icon: <Wrench size={13} />,     color: 'text-violet-400',  bg: 'bg-violet-950/40 border-violet-800/50',   label: '已打补丁' },
      command: { icon: <ArrowRight size={13} />, color: 'text-indigo-400',  bg: 'bg-indigo-950/40 border-indigo-800/50',   label: '指令' },
    }
    const s = statusConfig[message.workflow_status ?? 'success']
    return (
      <div className="flex justify-center my-2 px-4 animate-fade-in">
        <div className={`flex items-start gap-3 w-full max-w-2xl border rounded-xl px-4 py-3 ${s.bg}`}>
          <span className="text-xl mt-0.5 flex-shrink-0">⚙️</span>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <span className="text-xs font-bold text-slate-300">工作流</span>
              {/* from → to */}
              {message.workflow_from && (
                <span className="flex items-center gap-1 text-xs text-slate-400">
                  <span className="bg-slate-700 rounded px-1.5 py-0.5">{message.workflow_from}</span>
                  <ArrowRight size={11} className="text-slate-500" />
                  <span className="bg-slate-700 rounded px-1.5 py-0.5">{message.workflow_to ?? '—'}</span>
                </span>
              )}
              {/* 状态徽标 */}
              <span className={`flex items-center gap-1 text-xs font-medium ${s.color}`}>
                {s.icon}{s.label}
              </span>
              <span className="ml-auto text-slate-600 text-xs">{formatTime(message.timestamp)}</span>
            </div>
            {message.workflow_task && (
              <p className="text-xs text-slate-400 truncate">{message.workflow_task}</p>
            )}
            {message.content && (
              <p className="text-sm text-slate-300 mt-1 whitespace-pre-line">{message.content}</p>
            )}
          </div>
        </div>
      </div>
    )
  }

  if (isSystem) {
    return (
      <div className="flex justify-center my-3 animate-fade-in">
        <div className="bg-slate-800/60 border border-slate-700/50 text-slate-400 text-xs px-4 py-2 rounded-full max-w-lg text-center">
          {message.content}
        </div>
      </div>
    )
  }

  if (isRoute) {
    return (
      <div className="flex items-start gap-2 my-2 px-4 animate-fade-in">
        <span className="text-lg mt-0.5 flex-shrink-0">{message.sender_avatar}</span>
        <div className="bg-indigo-950/60 border border-indigo-800/50 rounded-xl px-4 py-2 max-w-xl">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-semibold" style={{ color: message.sender_color }}>
              {message.sender_name}
            </span>
            <span className="text-slate-600 text-xs">·</span>
            <span className="text-slate-500 text-xs">路由决策</span>
            <span className="ml-auto text-slate-600 text-xs">{formatTime(message.timestamp)}</span>
          </div>
          <p className="text-indigo-300 text-sm whitespace-pre-line">{message.content}</p>
        </div>
      </div>
    )
  }

  if (isUser) {
    return (
      <div className="flex justify-end items-end gap-2 px-4 my-2 animate-slide-up group">
        <div className="flex flex-col items-end gap-1 max-w-[70%]">
          <div className="bg-indigo-600 text-white rounded-2xl rounded-br-sm px-4 py-3 shadow-lg">
            <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
              <HighlightMentions text={message.content} />
            </p>
          </div>
          <div className="flex items-center gap-1">
            <CopyButton text={message.content} />
            <span className="text-slate-600 text-xs">{formatTime(message.timestamp)}</span>
          </div>
        </div>
        <div className="w-8 h-8 rounded-full bg-slate-700 flex items-center justify-center text-sm flex-shrink-0 shadow">
          👤
        </div>
      </div>
    )
  }

  // Agent 消息
  return (
    <div className="flex items-start gap-3 px-4 my-2 animate-slide-up group">
      {/* 头像 */}
      <div
        className="w-9 h-9 rounded-full flex items-center justify-center text-lg flex-shrink-0 shadow-lg border border-white/10"
        style={{ background: `${message.sender_color}22` }}
      >
        {message.sender_avatar}
      </div>

      {/* 内容区 */}
      <div className="flex flex-col gap-1 max-w-[78%]">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold" style={{ color: message.sender_color }}>
            {message.sender_name}
          </span>
          <span className="text-slate-600 text-xs">{formatTime(message.timestamp)}</span>
          {!message.streaming && <CopyButton text={message.content} />}
        </div>

        <div
          className="bg-slate-800/80 border border-slate-700/50 rounded-2xl rounded-tl-sm px-4 py-3 shadow backdrop-blur-sm"
          style={{ borderLeftColor: `${message.sender_color}55`, borderLeftWidth: 2 }}
        >
          {message.content ? (
            <div className={`prose-chat text-sm text-slate-200 ${message.streaming ? 'stream-cursor' : ''}`}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {message.content}
              </ReactMarkdown>
            </div>
          ) : (
            <div className="text-slate-500 text-sm italic">正在生成...</div>
          )}
        </div>
      </div>
    </div>
  )
}
