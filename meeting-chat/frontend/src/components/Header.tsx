import { Users, Wifi, WifiOff, Trash2 } from 'lucide-react'

interface Props {
  meetingId: string
  agentCount: number
  connected: boolean
  userName: string
  editingName: boolean
  tempName: string
  onToggleSidebar: () => void
  onEditName: () => void
  onSaveName: () => void
  onCancelEdit: () => void
  onTempNameChange: (val: string) => void
  onClearHistory: () => void
}

export default function Header({
  meetingId,
  agentCount,
  connected,
  userName,
  editingName,
  tempName,
  onToggleSidebar,
  onEditName,
  onSaveName,
  onCancelEdit,
  onTempNameChange,
  onClearHistory,
}: Props) {
  return (
    <header className="h-14 bg-slate-900/90 border-b border-slate-800 flex items-center px-4 gap-3 flex-shrink-0 backdrop-blur-sm">
      <button
        onClick={onToggleSidebar}
        className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
        title="切换侧边栏"
      >
        <Users size={18} />
      </button>

      <div className="flex items-center gap-2 flex-1 min-w-0">
        <span className="text-lg">🎙️</span>
        <div className="min-w-0">
          <h1 className="text-sm font-bold text-slate-100 truncate">多Agent 智能会议室</h1>
          <p className="text-xs text-slate-500">#{meetingId} · {agentCount} 位AI参与者</p>
        </div>
      </div>

      <div className="flex items-center gap-2 flex-shrink-0">
        {editingName ? (
          <div className="flex items-center gap-1">
            <input
              autoFocus
              value={tempName}
              onChange={e => onTempNameChange(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') onSaveName()
                if (e.key === 'Escape') onCancelEdit()
              }}
              className="bg-slate-800 border border-slate-600 rounded-lg px-2 py-1 text-xs text-slate-200 w-20 outline-none focus:border-indigo-500"
            />
            <button onClick={onSaveName} className="text-xs text-emerald-400 hover:text-emerald-300 px-1">✓</button>
          </div>
        ) : (
          <button
            onClick={onEditName}
            className="text-xs text-slate-400 hover:text-slate-200 bg-slate-800/50 hover:bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1 transition-colors"
            title="修改昵称"
          >
            👤 {userName}
          </button>
        )}

        <div className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border transition-colors ${
          connected
            ? 'text-emerald-400 bg-emerald-950/40 border-emerald-800/50'
            : 'text-amber-400 bg-amber-950/40 border-amber-800/50 animate-pulse'
        }`}>
          {connected ? <Wifi size={12} /> : <WifiOff size={12} />}
          {connected ? '已连接' : '重连中...'}
        </div>

        <button
          onClick={onClearHistory}
          className="p-1.5 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          title="清空会议记录"
        >
          <Trash2 size={16} />
        </button>
      </div>
    </header>
  )
}
