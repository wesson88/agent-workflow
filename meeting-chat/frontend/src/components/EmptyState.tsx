const QUICK_QUESTIONS = [
  '如何设计一个高并发的秒杀系统？',
  '帮我规划一个新功能的开发流程',
  '前端性能优化有哪些最佳实践？',
  '如何制定测试策略和质量保障体系？',
]

interface Props {
  onSelect: (q: string) => void
}

export default function EmptyState({ onSelect }: Props) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-8">
      <div className="text-5xl mb-4">🎙️</div>
      <h2 className="text-xl font-bold text-slate-300 mb-2">欢迎来到 AI 会议室</h2>
      <p className="text-slate-500 text-sm max-w-md leading-relaxed">
        您可以在这里与AI专家团队讨论任何技术或产品问题。<br />
        主持人会智能分析您的问题并路由给最合适的专家。<br />
        <span className="text-indigo-400">
          也可以用 <code className="bg-slate-800 px-1 rounded">@专家名</code> 直接点名
        </span>
      </p>
      <div className="mt-6 grid grid-cols-2 gap-2 max-w-sm">
        {QUICK_QUESTIONS.map(q => (
          <button
            key={q}
            onClick={() => onSelect(q)}
            className="text-left bg-slate-800/60 hover:bg-slate-800 border border-slate-700/50 hover:border-slate-600 rounded-xl px-3 py-2.5 text-xs text-slate-400 hover:text-slate-200 transition-all"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  )
}
