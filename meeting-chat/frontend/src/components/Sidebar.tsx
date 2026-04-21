import type { AgentInfo, AgentStat } from '../types'

// Provider → 显示标签 + 颜色
const PROVIDER_LABELS: Record<string, { label: string; color: string; short: string }> = {
  deepseek: { label: 'DeepSeek', color: '#4ade80', short: 'DS' },
  claude:   { label: 'Claude',   color: '#fb923c', short: 'CL' },
  codex:    { label: 'Codex',    color: '#60a5fa', short: 'CX' },
  gemini:   { label: 'Gemini',   color: '#f472b6', short: 'GM' },
}

interface Props {
  agents: AgentInfo[]
  agentStats: Record<string, AgentStat>
  meetingId: string
}

function formatTime(ts: number) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function Sidebar({ agents, agentStats, meetingId }: Props) {
  const moderator = agents.find(a => a.role === 'moderator')
  const experts = agents.filter(a => a.role === 'expert')
  const totalMessages = Object.values(agentStats).reduce((s, v) => s + v.messageCount, 0)

  return (
    <aside className="w-64 bg-slate-900/80 border-r border-slate-800 flex flex-col h-full">
      {/* 会议室信息 */}
      <div className="p-4 border-b border-slate-800">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xl">🎙️</span>
          <div>
            <h2 className="font-bold text-slate-100 text-sm leading-tight">AI 会议室</h2>
            <p className="text-slate-500 text-xs">#{meetingId}</p>
          </div>
        </div>
        <div className="flex items-center justify-between">
          <div className="bg-emerald-950/50 border border-emerald-800/40 rounded-lg px-2.5 py-1">
            <span className="text-emerald-400 text-xs font-medium">🟢 进行中</span>
          </div>
          {totalMessages > 0 && (
            <span className="text-slate-500 text-xs">{totalMessages} 条发言</span>
          )}
        </div>
      </div>

      {/* 主持人 */}
      {moderator && (
        <div className="px-3 pt-3 pb-2">
          <p className="text-slate-500 text-xs font-semibold uppercase tracking-wider mb-2 px-1">主持人</p>
          <AgentCard agent={moderator} stat={agentStats[moderator.id]} highlight />
        </div>
      )}

      {/* 专家团队 */}
      <div className="px-3 pt-1 pb-4 flex-1 overflow-y-auto">
        <p className="text-slate-500 text-xs font-semibold uppercase tracking-wider mb-2 px-1">
          专家团队 · {experts.length}人
        </p>
        <div className="flex flex-col gap-1.5">
          {experts.map(agent => (
            <AgentCard key={agent.id} agent={agent} stat={agentStats[agent.id]} />
          ))}
        </div>
      </div>

      {/* 底部说明 */}
      <div className="p-3 border-t border-slate-800">
        <p className="text-slate-600 text-xs leading-relaxed">
          💡 主持人智能路由 · 支持 @点名
        </p>
      </div>
    </aside>
  )
}

function AgentCard({ agent, stat, highlight }: { agent: AgentInfo; stat?: AgentStat; highlight?: boolean }) {
  const provider = PROVIDER_LABELS[agent.provider ?? ''] ?? { label: agent.provider ?? '?', color: '#94a3b8', short: '??' }
  const isActive = stat?.isTyping || stat?.isStreaming
  const isStreaming = stat?.isStreaming

  return (
    <div
      className={`rounded-xl px-3 py-2.5 border transition-all ${
        isActive
          ? 'bg-slate-800/80 border-slate-600 shadow-md'
          : highlight
          ? 'bg-indigo-950/60 border-indigo-800/50'
          : 'bg-slate-800/30 border-slate-700/30 hover:bg-slate-800/60'
      }`}
      style={isActive ? { borderColor: `${agent.color}66` } : undefined}
    >
      {/* 行1：头像 + 名字 + 状态点 */}
      <div className="flex items-center gap-2">
        <div
          className="w-8 h-8 rounded-full flex items-center justify-center text-base flex-shrink-0 relative"
          style={{ background: `${agent.color}22` }}
        >
          {agent.avatar}
          {/* 实时状态指示圈 */}
          <span
            className={`absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full border-2 border-slate-900 ${
              isStreaming
                ? 'bg-yellow-400 animate-pulse'
                : isActive
                ? 'bg-blue-400 animate-pulse'
                : stat?.messageCount
                ? 'bg-emerald-400'
                : 'bg-slate-600'
            }`}
          />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <p className="text-sm font-medium text-slate-200 truncate leading-tight">{agent.name}</p>
            {/* 消息计数徽章 */}
            {(stat?.messageCount ?? 0) > 0 && (
              <span
                className="text-xs px-1.5 py-0 rounded-full font-semibold flex-shrink-0"
                style={{ background: `${agent.color}22`, color: agent.color }}
              >
                {stat!.messageCount}
              </span>
            )}
          </div>
          {/* Provider 标签 */}
          <div className="flex items-center gap-1 mt-0.5">
            <span
              className="text-xs font-mono px-1 py-0 rounded"
              style={{ background: `${provider.color}18`, color: provider.color }}
            >
              {provider.short}
            </span>
            <span className="text-xs truncate" style={{ color: provider.color + 'aa' }}>
              {provider.label}
            </span>
          </div>
        </div>
      </div>

      {/* 行2：实时状态 / 最后发言 */}
      {isStreaming && (
        <div className="mt-2 flex items-center gap-1.5">
          <span className="flex gap-0.5">
            {[0,1,2].map(i => (
              <span
                key={i}
                className="w-1 h-1 rounded-full bg-yellow-400 animate-bounce"
                style={{ animationDelay: `${i * 0.15}s` }}
              />
            ))}
          </span>
          <span className="text-xs text-yellow-400/80">正在生成回复...</span>
        </div>
      )}
      {stat?.isTyping && !isStreaming && (
        <div className="mt-2 flex items-center gap-1.5">
          <span className="flex gap-0.5">
            {[0,1,2].map(i => (
              <span
                key={i}
                className="w-1 h-1 rounded-full bg-blue-400 animate-bounce"
                style={{ animationDelay: `${i * 0.15}s` }}
              />
            ))}
          </span>
          <span className="text-xs text-blue-400/80">思考中...</span>
        </div>
      )}
      {!isActive && stat?.lastContent && (
        <div className="mt-1.5">
          <p className="text-xs text-slate-500 truncate leading-relaxed">{stat.lastContent}</p>
          <p className="text-xs text-slate-600 mt-0.5">{formatTime(stat.lastTime)}</p>
        </div>
      )}
    </div>
  )
}
