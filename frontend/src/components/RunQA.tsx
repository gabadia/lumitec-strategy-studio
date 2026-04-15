import { useState, useRef, useCallback, useEffect } from 'react'
import { getTraderHeaders } from '../auth'
import type { ActivityEntry } from '../types'

interface Message {
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}

interface Props {
  activity: ActivityEntry[]
  code: string
  modelSettings: { generateModel: string }
}

function buildContext(activity: ActivityEntry[], code: string): string {
  const parts: string[] = []

  if (code.trim()) {
    parts.push(`STRATEGY CODE:\n\`\`\`python\n${code.trim()}\n\`\`\``)
  }

  const relevant = activity.filter(e =>
    e.type === 'text_delta' ||
    e.type === 'sim_commentary' ||
    (e.type === 'tool_result' && (e.toolName === 'stream_events' || e.toolName === 'get_strategy_status'))
  )

  const eventLines: string[] = []
  for (const e of relevant) {
    if (e.type === 'text_delta' && e.content) {
      eventLines.push(`[AGENT]: ${e.content.trim()}`)
    } else if (e.type === 'sim_commentary' && e.simCommentary) {
      eventLines.push(`[MONITOR]: ${e.simCommentary.trim()} | position=${e.simPosition ?? 0} pnl=${e.simPnl ?? 0}`)
    } else if (e.type === 'tool_result' && e.toolContent) {
      eventLines.push(`[${e.toolName?.toUpperCase()}]: ${e.toolContent.trim().slice(0, 800)}`)
    }
  }

  if (eventLines.length > 0) {
    parts.push(`RUN EVENTS:\n${eventLines.join('\n')}`)
  }

  return parts.join('\n\n')
}

export default function RunQA({ activity, code, modelSettings }: Props) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const sendQuestion = useCallback(async () => {
    const q = input.trim()
    if (!q || busy) return
    setInput('')
    setBusy(true)

    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(prev => [
      ...prev,
      { role: 'user', content: q },
      { role: 'assistant', content: '', streaming: true },
    ])

    try {
      const response = await fetch('/api/ask-run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
        body: JSON.stringify({
          question: q,
          history,
          context: buildContext(activity, code),
          model: modelSettings.generateModel,
        }),
      })

      if (!response.body) throw new Error('No response body')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const evt = JSON.parse(line.slice(6))
            if (evt.type === 'text_delta') {
              setMessages(prev => {
                const next = [...prev]
                const last = next[next.length - 1]
                if (last?.role === 'assistant') {
                  next[next.length - 1] = { ...last, content: last.content + evt.delta }
                }
                return next
              })
            }
          } catch { /* ignore */ }
        }
      }
    } catch (err) {
      setMessages(prev => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last?.role === 'assistant') {
          next[next.length - 1] = { ...last, content: `Error: ${err}` }
        }
        return next
      })
    } finally {
      setMessages(prev => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last?.role === 'assistant') {
          next[next.length - 1] = { ...last, streaming: false }
        }
        return next
      })
      setBusy(false)
    }
  }, [input, busy, messages, activity, code, modelSettings])

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      sendQuestion()
    }
  }, [sendQuestion])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{ padding: '8px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-dim)', fontSize: 11, fontFamily: 'var(--font-mono)', letterSpacing: '0.1em', display: 'flex', alignItems: 'center', gap: 8 }}>
        RUN Q&amp;A
        {messages.length > 0 && (
          <button onClick={() => setMessages([])} style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--text-muted)', background: 'transparent', border: 'none', cursor: 'pointer' }}>
            clear
          </button>
        )}
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        {messages.length === 0 && (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic' }}>
            Ask anything about this run — quotes received, signal values, why no fills, etc.
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' }}>
              {m.role === 'user' ? 'YOU' : 'ANALYST'}
            </div>
            <div style={{
              fontSize: 12, lineHeight: 1.6,
              color: m.role === 'user' ? 'var(--text)' : 'var(--text-dim)',
              background: m.role === 'user' ? 'var(--surface-2)' : 'transparent',
              padding: m.role === 'user' ? '6px 8px' : '0',
              borderRadius: 4,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}>
              {m.content || (m.streaming ? '…' : '')}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div style={{ padding: '8px 10px', borderTop: '1px solid var(--border)', display: 'flex', gap: 6 }}>
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about the run… (⌘↵ to send)"
          rows={2}
          disabled={busy}
          style={{
            flex: 1, fontSize: 12, lineHeight: 1.5, resize: 'none',
            color: 'var(--text)', background: 'var(--surface)',
            border: '1px solid var(--border)', borderRadius: 4, padding: '5px 8px',
          }}
        />
        <button
          onClick={sendQuestion}
          disabled={!input.trim() || busy}
          style={{
            padding: '0 12px', borderRadius: 4, fontSize: 12, fontWeight: 600, border: 'none',
            background: input.trim() && !busy ? 'var(--accent)' : 'var(--surface)',
            color: input.trim() && !busy ? '#fff' : 'var(--text-muted)',
            cursor: input.trim() && !busy ? 'pointer' : 'not-allowed',
            alignSelf: 'stretch',
          }}
        >
          {busy ? '…' : 'Ask'}
        </button>
      </div>
    </div>
  )
}
