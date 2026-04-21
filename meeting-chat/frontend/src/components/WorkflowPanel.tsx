import { useState, useEffect, useCallback } from 'react'
import { Play, Pause, StopCircle, Zap, RefreshCw, ChevronDown, ChevronUp, AlertTriangle } from 'lucide-react'
import type { StatusJsonSnapshot, SkillRegistryEntry } from '../types'

// skill 与 Agent 的映射（显示用）
const SKILL_LABELS: Record<string, { name: string; avatar: string; color: string }> = {
  chief_architect: { name: '首席架构师', avatar: '🏗️', color: '#0ea5e9' },
  technical_lead:  { name: '技术主管',   avatar: '🧑‍💼', color: '#06b6d4' },
  dev_backend:     { name: '后端工程师', avatar: '⚙️',  color: '#10b981' },
  dev_frontend:    { name: '前端工程师', avatar: '🎨',  color: '#f59e0b' },
  meeting_chat:    { name: '会议系统',   avatar: '🎙️', color: '#6366f1' },
}

const SKILL_CHAIN = ['chief_architect', 'technical_lead', 'dev_backend', 'dev_frontend']

interface InboxStatus {
  paused: string[]
  pending: Record<string, number>
}

interface Props {
  status: StatusJsonSnapshot | null
  onSendCommand: (skill: string, command: string) => void
}

// status.json 的 skill_registry 状态 → 样式类名
function statusClasses(s: SkillRegistryEntry | undefined, paused: boolean): string {
  if (paused) return 'bg-amber-950/50 border-amber-700/50 text-amber-300'
  const v = s?.status
  if (v === 'busy')    return 'bg-indigo-950/50 border-indigo-700/50 text-indigo-300'
  if (v === 'success') return 'bg-emerald-950/50 border-emerald-700/50 text-emerald-300'
  if (v === 'failed')  return 'bg-red-950/50 border-red-700/50 text-red-300'
  if (v === 'blocked') return 'bg-orange-950/50 border-orange-700/50 text-orange-300'
  return 'bg-slate-800/60 border-slate-700/40 text-slate-400'
}

export default function WorkflowPanel({ status, onSendCommand }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [inbox, setInbox] = useState<InboxStatus>({ paused: [], pending: {} })
  const [injectTarget, setInjectTarget] = useState('chief_architect')
  const [injectText, setInjectText] = useState('')
  const [taskTarget, setTaskTarget] = useState('chief_architect')
  const [taskText, setTaskText] = useState('')

  // 仅首次加载与展开时拉一次 inbox 状态（paused/pending 仍走 REST）
  useEffect(() => {
    if (!expanded) return
    const fetch_ = () => {
      fetch('/api/workflow/status')
        .then(r => r.json())
        .then(setInbox)
        .catch(() => {})
    }
    fetch_()
    const id = setInterval(fetch_, 3000)
    return () => clearInterval(id)
  }, [expanded])

  const sendCmd = useCallback((skill: string, cmd: string) => {
    onSendCommand(skill, cmd)
    if (cmd === 'pause')  setInbox(s => ({ ...s, paused: [...new Set([...s.paused, skill])] }))
    if (cmd === 'resume') setInbox(s => ({ ...s, paused: s.paused.filter(x => x !== skill) }))
  }, [onSendCommand])

  const sendInject = () => {
    if (!injectText.trim()) return
    sendCmd(injectTarget, `inject:${injectText.trim()}`)
    setInjectText('')
  }

  const sendSetTask = () => {
    if (!taskText.trim()) return
    sendCmd(taskTarget, `set_task:${taskText.trim()}`)
    setTaskText('')
  }

  const systemBlocked = status?.system_state === 'blocked'
  const hasBusy       = status ? Object.values(status.skill_registry || {}).some(s => s.status === 'busy') : false

  return (
    <div className="border-t border-slate-800 bg-slate-900/60">
      {/* 全局阻塞警示条 */}
      {systemBlocked && (
        <div className="flex items-center gap-2 px-4 py-2 bg-orange-950/60 border-b border-orange-800/50 text-orange-300 text-xs">
          <AlertTriangle size={14} />
          <span className="font-medium">系统阻塞</span>
          {status?.block_reason && <span className="text-orange-400/80">· {status.block_reason}</span>}
        </div>
      )}

      {/* 折叠标题栏 */}
      <button
        onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center gap-2 px-4 py-2.5 text-left hover:bg-slate-800/40 transition-colors"
      >
        <span className="text-sm">⚙️</span>
        <span className="text-xs font-semibold text-slate-400 flex-1">工作流控制台</span>
        {hasBusy && (
          <span className="text-xs bg-indigo-900/60 text-indigo-400 border border-indigo-700/50 rounded-full px-2 py-0.5 flex items-center gap-1">
            <RefreshCw size={10} className="animate-spin" /> 运行中
          </span>
        )}
        {inbox.paused.length > 0 && (
          <span className="text-xs bg-amber-900/60 text-amber-400 border border-amber-700/50 rounded-full px-2 py-0.5">
            {inbox.paused.length} 已暂停
          </span>
        )}
        {Object.keys(inbox.pending).length > 0 && (
          <span className="text-xs bg-indigo-900/60 text-indigo-400 border border-indigo-700/50 rounded-full px-2 py-0.5">
            待处理
          </span>
        )}
        {expanded ? <ChevronUp size={14} className="text-slate-500" /> : <ChevronDown size={14} className="text-slate-500" />}
      </button>

      {expanded && (
        <div className="px-4 pb-4 space-y-4">

          {/* Skill 流水线状态 */}
          <div>
            <p className="text-xs text-slate-500 mb-2 flex items-center gap-2">
              <span>流水线</span>
              {status?.project_name && (
                <span className="text-slate-600">· {status.project_name}</span>
              )}
            </p>
            <div className="flex items-center gap-1 flex-wrap">
              {SKILL_CHAIN.map((skill, idx) => {
                const meta    = SKILL_LABELS[skill]
                const entry   = status?.skill_registry?.[skill]
                const paused  = inbox.paused.includes(skill)
                const pending = (inbox.pending[skill] ?? 0) > 0
                const failures = entry?.consecutive_failures ?? 0
                const tipParts = [
                  entry?.status && `状态: ${entry.status}`,
                  entry?.last_run && `上次运行: ${entry.last_run}`,
                  entry?.last_output_path && `产物: ${entry.last_output_path}`,
                  failures > 0 && `连续失败: ${failures}`,
                ].filter(Boolean)
                const tip = tipParts.join('\n')
                return (
                  <div key={skill} className="flex items-center gap-1">
                    <div
                      title={tip}
                      className={`relative flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 border text-xs transition-colors ${statusClasses(entry, paused)}`}
                    >
                      <span>{meta.avatar}</span>
                      <span className="hidden sm:inline">{meta.name}</span>
                      {paused && <Pause size={10} className="text-amber-400" />}
                      {!paused && entry?.status === 'busy'    && <RefreshCw size={10} className="animate-spin" />}
                      {!paused && entry?.status === 'success' && <span className="text-emerald-400">✓</span>}
                      {!paused && entry?.status === 'blocked' && <AlertTriangle size={10} className="text-orange-400" />}
                      {pending && !paused && <RefreshCw size={10} className="animate-spin" />}
                      {failures > 0 && (
                        <span className="absolute -top-1.5 -right-1.5 bg-red-600 text-white text-[10px] leading-none min-w-[16px] h-[16px] px-1 rounded-full flex items-center justify-center">
                          {failures}
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => sendCmd(skill, paused ? 'resume' : 'pause')}
                      className={`p-1 rounded text-xs transition-colors ${
                        paused
                          ? 'text-emerald-400 hover:bg-emerald-900/30'
                          : 'text-slate-500 hover:text-amber-400 hover:bg-amber-900/30'
                      }`}
                      title={paused ? '恢复' : '暂停'}
                    >
                      {paused ? <Play size={11} /> : <Pause size={11} />}
                    </button>
                    {idx < SKILL_CHAIN.length - 1 && (
                      <span className="text-slate-700 text-xs">→</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          {/* 快捷全局操作 */}
          <div className="flex gap-2 flex-wrap">
            <button
              onClick={() => SKILL_CHAIN.forEach(s => sendCmd(s, 'pause'))}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-amber-950/50 border border-amber-700/50 text-amber-300 hover:bg-amber-900/50 transition-colors"
            >
              <Pause size={12} /> 全部暂停
            </button>
            <button
              onClick={() => SKILL_CHAIN.forEach(s => sendCmd(s, 'resume'))}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-emerald-950/50 border border-emerald-700/50 text-emerald-300 hover:bg-emerald-900/50 transition-colors"
            >
              <Play size={12} /> 全部恢复
            </button>
            <button
              onClick={() => {
                const t = injectTarget
                if (window.confirm(`确认中止 [${SKILL_LABELS[t]?.name}] 当前执行？`))
                  sendCmd(t, 'abort')
              }}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-red-950/50 border border-red-700/50 text-red-300 hover:bg-red-900/50 transition-colors"
            >
              <StopCircle size={12} /> 中止
            </button>
          </div>

          {/* 注入新指令 */}
          <div className="space-y-2">
            <p className="text-xs text-slate-500 flex items-center gap-1">
              <Zap size={11} /> 向 skill.md 注入指令
            </p>
            <div className="flex gap-2">
              <select
                value={injectTarget}
                onChange={e => setInjectTarget(e.target.value)}
                className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-300 outline-none focus:border-indigo-500"
              >
                {SKILL_CHAIN.map(s => (
                  <option key={s} value={s}>{SKILL_LABELS[s].avatar} {SKILL_LABELS[s].name}</option>
                ))}
              </select>
              <input
                value={injectText}
                onChange={e => setInjectText(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && sendInject()}
                placeholder="输入指令，Enter 发送..."
                className="flex-1 bg-slate-800 border border-slate-700 focus:border-indigo-500 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 outline-none"
              />
              <button
                onClick={sendInject}
                disabled={!injectText.trim()}
                className="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-700 disabled:text-slate-500 text-white text-xs transition-colors"
              >
                注入
              </button>
            </div>
          </div>

          {/* 替换任务 */}
          <div className="space-y-2">
            <p className="text-xs text-slate-500 flex items-center gap-1">
              <RefreshCw size={11} /> 替换当前任务
            </p>
            <div className="flex gap-2">
              <select
                value={taskTarget}
                onChange={e => setTaskTarget(e.target.value)}
                className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-300 outline-none focus:border-indigo-500"
              >
                {SKILL_CHAIN.map(s => (
                  <option key={s} value={s}>{SKILL_LABELS[s].avatar} {SKILL_LABELS[s].name}</option>
                ))}
              </select>
              <input
                value={taskText}
                onChange={e => setTaskText(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && sendSetTask()}
                placeholder="新任务描述..."
                className="flex-1 bg-slate-800 border border-slate-700 focus:border-indigo-500 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 outline-none"
              />
              <button
                onClick={sendSetTask}
                disabled={!taskText.trim()}
                className="px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 disabled:bg-slate-700 disabled:text-slate-500 text-white text-xs transition-colors"
              >
                替换
              </button>
            </div>
          </div>

        </div>
      )}
    </div>
  )
}
