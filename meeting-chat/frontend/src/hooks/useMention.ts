import { useCallback, useState } from 'react'
import type { AgentInfo } from '../types'

export interface SlashTarget {
  id: string          // 'all' | 'chief_architect' | 'technical_lead' | 'dev_backend' | 'dev_frontend'
  label: string       // 显示名
  avatar: string
}

const SLASH_TARGETS: SlashTarget[] = [
  { id: 'all',              label: '全链路（optimize_all）', avatar: '🚀' },
  { id: 'chief_architect',  label: '首席架构师',              avatar: '🏗️' },
  { id: 'technical_lead',   label: '技术主管',                 avatar: '🧑‍💼' },
  { id: 'dev_backend',      label: '后端工程师',              avatar: '⚙️' },
  { id: 'dev_frontend',     label: '前端工程师',              avatar: '🎨' },
]

export function useMention(
  agents: AgentInfo[],
  input: string,
  setInput: (val: string) => void,
  textareaRef: React.RefObject<HTMLTextAreaElement>,
) {
  const [mentionList, setMentionList] = useState<AgentInfo[]>([])
  const [mentionQuery, setMentionQuery] = useState('')
  const [slashList, setSlashList] = useState<SlashTarget[]>([])

  const detectMention = useCallback((val: string) => {
    // 斜杠命令优先（仅当整个输入以 /run 开头且尚未填入 target）
    const slashMatch = val.match(/^\/run(?:\s+(\S*))?$/)
    if (slashMatch) {
      const query = (slashMatch[1] ?? '').toLowerCase()
      const filtered = query
        ? SLASH_TARGETS.filter(t => t.id.includes(query) || t.label.toLowerCase().includes(query))
        : SLASH_TARGETS
      setSlashList(filtered)
      setMentionList([])
      setMentionQuery('')
      return
    }
    setSlashList([])

    const atIdx = val.lastIndexOf('@')
    if (atIdx < 0) {
      setMentionList([])
      return
    }
    if (atIdx === val.length - 1) {
      setMentionList(agents.filter(a => a.role === 'expert'))
      setMentionQuery('')
      return
    }
    const query = val.slice(atIdx + 1)
    if (/\s/.test(query)) {
      setMentionList([])
      return
    }
    const filtered = agents.filter(
      a =>
        a.role === 'expert' &&
        (a.name.includes(query) || a.id.includes(query.toLowerCase())),
    )
    setMentionList(filtered)
    setMentionQuery(query)
  }, [agents])

  const pickMention = useCallback((agent: AgentInfo) => {
    const atIdx = input.lastIndexOf('@')
    const newInput = input.slice(0, atIdx) + `@${agent.name} `
    setInput(newInput)
    setMentionList([])
    textareaRef.current?.focus()
  }, [input, setInput, textareaRef])

  const pickSlash = useCallback((target: SlashTarget) => {
    setInput(`/run ${target.id} `)
    setSlashList([])
    textareaRef.current?.focus()
  }, [setInput, textareaRef])

  const closeMention = useCallback(() => {
    setMentionList([])
    setSlashList([])
  }, [])

  return {
    mentionList,
    mentionQuery,
    slashList,
    detectMention,
    pickMention,
    pickSlash,
    closeMention,
  }
}
