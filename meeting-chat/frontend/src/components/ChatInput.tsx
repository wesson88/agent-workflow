import { RefreshCw, AtSign, Send } from 'lucide-react'
import type { AgentInfo } from '../types'
import type { SlashTarget } from '../hooks/useMention'

interface Props {
  input: string
  connected: boolean
  processing: boolean
  mentionList: AgentInfo[]
  slashList: SlashTarget[]
  textareaRef: React.RefObject<HTMLTextAreaElement>
  onChange: (val: string) => void
  onKeyDown: (e: React.KeyboardEvent) => void
  onSend: () => void
  onPickMention: (agent: AgentInfo) => void
  onPickSlash: (target: SlashTarget) => void
  onAtClick: () => void
}

export default function ChatInput({
  input,
  connected,
  processing,
  mentionList,
  slashList,
  textareaRef,
  onChange,
  onKeyDown,
  onSend,
  onPickMention,
  onPickSlash,
  onAtClick,
}: Props) {
  return (
    <footer className="border-t border-slate-800 bg-slate-900/80 backdrop-blur-sm p-4 flex-shrink-0">
      {processing && (
        <div className="flex items-center gap-2 mb-3 text-xs text-indigo-400">
          <RefreshCw size={12} className="animate-spin" />
          <span>专家们正在讨论中...</span>
        </div>
      )}

      {slashList.length > 0 && (
        <div className="mb-2 bg-slate-800 border border-slate-700 rounded-xl overflow-hidden shadow-xl">
          <p className="text-slate-500 text-xs px-3 pt-2 pb-1">触发工作流：</p>
          {slashList.map(t => (
            <button
              key={t.id}
              onClick={() => onPickSlash(t)}
              className="w-full flex items-center gap-2.5 px-3 py-2 hover:bg-slate-700 transition-colors text-left"
            >
              <span className="text-base">{t.avatar}</span>
              <span className="text-sm text-slate-200">{t.label}</span>
              <span className="text-xs text-slate-500 ml-auto">/run {t.id}</span>
            </button>
          ))}
        </div>
      )}

      {mentionList.length > 0 && (
        <div className="mb-2 bg-slate-800 border border-slate-700 rounded-xl overflow-hidden shadow-xl">
          <p className="text-slate-500 text-xs px-3 pt-2 pb-1">点名专家：</p>
          {mentionList.map(agent => (
            <button
              key={agent.id}
              onClick={() => onPickMention(agent)}
              className="w-full flex items-center gap-2.5 px-3 py-2 hover:bg-slate-700 transition-colors text-left"
            >
              <span className="text-base">{agent.avatar}</span>
              <span className="text-sm text-slate-200">{agent.name}</span>
              <span className="text-xs text-slate-500 ml-auto">@{agent.id}</span>
            </button>
          ))}
        </div>
      )}

      <div className="flex items-end gap-3">
        <button
          onClick={onAtClick}
          className="p-2.5 rounded-xl text-slate-500 hover:text-indigo-400 hover:bg-slate-800 transition-colors flex-shrink-0 mb-0.5"
          title="点名专家 (@mention)"
        >
          <AtSign size={18} />
        </button>

        <div className="flex-1 relative">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={e => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              connected
                ? '输入问题... (Enter 发送 · @点名专家 · /run 触发工作流)'
                : '正在重新连接...'
            }
            disabled={!connected || processing}
            rows={1}
            className="w-full bg-slate-800 border border-slate-700 focus:border-indigo-500 rounded-2xl px-4 py-3 text-sm text-slate-200 placeholder-slate-500 resize-none outline-none transition-colors disabled:opacity-50 disabled:cursor-not-allowed leading-relaxed"
            style={{ minHeight: '48px', maxHeight: '160px' }}
          />
        </div>

        <button
          onClick={onSend}
          disabled={!input.trim() || processing || !connected}
          className="w-11 h-11 rounded-2xl bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-700 disabled:text-slate-500 text-white flex items-center justify-center transition-all flex-shrink-0 shadow-lg hover:shadow-indigo-500/25 active:scale-95"
          title="发送 (Enter)"
        >
          <Send size={16} />
        </button>
      </div>

      <p className="mt-2 text-slate-600 text-xs text-center">
        由 AI 主持人协调 · 智能网关路由 · 多Agent并发响应 · 支持 @点名 直接指定专家
      </p>
    </footer>
  )
}
