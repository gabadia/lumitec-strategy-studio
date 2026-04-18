import { useEffect, useRef } from 'react'
import { useStore } from '../App'
import type { ActivityEntry } from '../types'

function formatTime(ts: number) {
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function truncate(s: string, max = 400) {
  return s.length > max ? s.slice(0, max) + '…' : s
}

function formatDuration(ms: number) {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

const EXECUTION_PLANE_TOOLS = new Set(['submit_strategy', 'start_strategy', 'stop_strategy', 'update_strategy', 'get_strategy_status'])

function toolLabel(name: string | undefined) {
  return name && EXECUTION_PLANE_TOOLS.has(name) ? 'REST' : 'MCP'
}

function EntryRow({ entry }: { entry: ActivityEntry }) {
  const { type } = entry

  if (type === 'tools_ready') {
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-dim)', fontSize: 11 }}>
        <span style={{ color: 'var(--text-muted)', marginRight: 8 }}>{formatTime(entry.timestamp)}</span>
        <span className="tag tag-result">MCP</span>
        <span style={{ marginLeft: 6 }}>{entry.content}</span>
      </div>
    )
  }

  if (type === 'thinking') {
    const modelLabel = entry.model
      ? entry.model.startsWith('gpt') ? 'GPT' : entry.model.startsWith('claude') ? 'Claude' : entry.model
      : 'Claude'
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
        <span className="tag tag-text">{modelLabel}</span>
        <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>turn {entry.turn}</span>
        <span style={{ display: 'flex', gap: 3, alignItems: 'center' }}>
          {[0, 1, 2].map(i => (
            <span key={i} style={{
              width: 4, height: 4, borderRadius: '50%',
              background: 'var(--accent)',
              animation: `pulse 1.2s ease-in-out ${i * 0.2}s infinite`,
            }} />
          ))}
        </span>
      </div>
    )
  }

  if (type === 'text_delta') {
    const modelLabel = entry.model
      ? entry.model.startsWith('gpt') ? 'GPT' : entry.model.startsWith('claude') ? 'Claude' : entry.model
      : 'GPT'
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
          <span className="tag tag-text">{modelLabel}</span>
          {entry.turn && <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>turn {entry.turn}</span>}
          {entry.live && <span style={{ width: 2, height: 12, background: 'var(--accent)', display: 'inline-block', animation: 'blink 1s step-end infinite' }} />}
        </div>
        <div style={{ color: 'var(--text)', fontSize: 12, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {entry.content}
        </div>
      </div>
    )
  }

  if (type === 'tool_call_start') {
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
        <span className="tag tag-tool">→ {toolLabel(entry.toolName)}</span>
        <span style={{ color: 'var(--accent)', fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 600 }}>
          {entry.toolName}
        </span>
        <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>building input…</span>
      </div>
    )
  }

  if (type === 'tool_call') {
    const inputStr = entry.toolInput ? JSON.stringify(entry.toolInput, null, 2) : ''
    const preview = truncate(inputStr.replace(/\\n/g, '\n'), 200)
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
          <span className="tag tag-tool">→ {toolLabel(entry.toolName)}</span>
          <span style={{ color: 'var(--accent)', fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 600 }}>
            {entry.toolName}
          </span>
        </div>
        {preview && (
          <pre style={{ marginTop: 3, fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
            {preview}
          </pre>
        )}
      </div>
    )
  }

  if (type === 'tool_executing') {
    return (
      <div style={{ padding: '4px 12px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
        <span className="tag tag-tool">⟳ {toolLabel(entry.toolName)}</span>
        <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>
          {entry.toolName} running…
        </span>
      </div>
    )
  }

  if (type === 'tool_result') {
    const failed = entry.toolFailed
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', background: failed ? '#1a0a0a' : 'transparent' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
          <span className="tag tag-result" style={failed ? { background: '#3a1a1a', color: 'var(--red)' } : {}}>← {toolLabel(entry.toolName)}</span>
          <span style={{ color: failed ? 'var(--red)' : 'var(--green)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
            {entry.toolName}
          </span>
          {entry.durationMs !== undefined && (
            <span style={{ color: 'var(--text-muted)', fontSize: 10, fontFamily: 'var(--font-mono)' }}>
              {formatDuration(entry.durationMs)}
            </span>
          )}
          {failed && <span style={{ color: 'var(--red)', fontSize: 10 }}>FAILED</span>}
        </div>
        {entry.toolContent && (
          <pre style={{ marginTop: 3, fontSize: 10, color: failed ? '#ff8888' : 'var(--text-dim)', fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: failed ? 300 : 120, overflow: 'auto' }}>
            {failed ? entry.toolContent : truncate(entry.toolContent)}
          </pre>
        )}
      </div>
    )
  }

  if (type === 'tool_error') {
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', background: '#1a0a0a' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
          <span className="tag tag-error">✗ {toolLabel(entry.toolName)}</span>
          <span style={{ color: 'var(--red)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>{entry.toolName}</span>
          {entry.durationMs !== undefined && (
            <span style={{ color: 'var(--text-muted)', fontSize: 10, fontFamily: 'var(--font-mono)' }}>
              {formatDuration(entry.durationMs)}
            </span>
          )}
        </div>
        <div style={{ marginTop: 2, fontSize: 11, color: 'var(--red)' }}>{entry.toolError}</div>
      </div>
    )
  }

  if (type === 'sim_commentary') {
    const hasViolations = entry.simViolations && entry.simViolations.length > 0
    const criticalViolations = entry.simViolations?.filter(v => v.severity === 'CRITICAL') ?? []
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', background: hasViolations ? '#0f1a0f' : 'transparent' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{formatTime(entry.timestamp)}</span>
          <span className="tag" style={{ background: '#0a2a2a', color: '#4ec9b0', border: '1px solid #1a4a4a' }}>~ AUDIT</span>
          {entry.simPosition !== undefined && (
            <span style={{ fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
              pos {entry.simPosition}
            </span>
          )}
          {entry.simPnl !== undefined && (
            <span style={{
              fontSize: 10,
              fontFamily: 'var(--font-mono)',
              color: entry.simPnl >= 0 ? 'var(--green)' : 'var(--red)',
            }}>
              P&L ${entry.simPnl.toFixed(2)}
            </span>
          )}
        </div>
        <div style={{ marginTop: 2, fontSize: 12, color: '#4ec9b0', lineHeight: 1.5 }}>
          {entry.simCommentary}
        </div>
        {criticalViolations.map((v, i) => (
          <div key={i} style={{ marginTop: 2, fontSize: 11, color: 'var(--red)', fontFamily: 'var(--font-mono)' }}>
            ⚠ {v.rule}: {v.detail}
          </div>
        ))}
      </div>
    )
  }

  if (type === 'rate_limit_retry') {
    return (
      <div style={{ padding: '5px 12px', borderBottom: '1px solid var(--border)', background: '#1a1500' }}>
        <span className="tag" style={{ background: '#3a2a00', color: 'var(--yellow)' }}>WAIT</span>
        <span style={{ marginLeft: 8, color: 'var(--yellow)', fontSize: 11 }}>{entry.content}</span>
      </div>
    )
  }

  if (type === 'error') {
    return (
      <div style={{ padding: '8px 12px', background: '#2a0a0a', borderBottom: '1px solid #3a1a1a' }}>
        <span className="tag tag-error">ERROR</span>
        <span style={{ marginLeft: 8, color: 'var(--red)', fontSize: 12 }}>{entry.content}</span>
      </div>
    )
  }

  return null
}

export default function ActivityFeed() {
  const activity = useStore((s) => s.activity)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [activity.length])

  if (!activity.length) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
        Agent activity will appear here
      </div>
    )
  }

  return (
    <>
      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 0.2; transform: scale(0.8); }
          50% { opacity: 1; transform: scale(1.2); }
        }
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
      `}</style>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {activity.map((entry) => (
          <EntryRow key={entry.id} entry={entry} />
        ))}
        <div ref={bottomRef} />
      </div>
    </>
  )
}
