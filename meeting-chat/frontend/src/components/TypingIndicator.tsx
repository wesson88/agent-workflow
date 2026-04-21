import type { TypingInfo } from '../types'

interface Props {
  agents: TypingInfo[]
}

export default function TypingIndicator({ agents }: Props) {
  if (agents.length === 0) return null

  return (
    <div className="px-4 pb-2 flex flex-col gap-1.5">
      {agents.map(agent => (
        <div key={agent.agent_id} className="flex items-center gap-2 animate-fade-in">
          <span className="text-base">{agent.agent_avatar}</span>
          <div className="bg-slate-800/70 border border-slate-700/40 rounded-xl px-3 py-2 flex items-center gap-2">
            <span className="text-slate-400 text-xs">{agent.agent_name} 正在输入</span>
            <span className="typing-dots text-indigo-400 flex gap-1">
              <span />
              <span />
              <span />
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}
