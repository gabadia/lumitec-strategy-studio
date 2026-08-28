import { useState, useRef, useCallback, useEffect } from 'react'
import { authHeaders } from '../auth/cognito'

interface Message {
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}

interface AnalysisPrompt {
  id: string
  label: string
  question: string
}

interface Props {
  strategyId: string | null
  modelSettings: { generateModel: string }
}

export default function RunQA({ strategyId, modelSettings }: Props) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [analysisPrompts, setAnalysisPrompts] = useState<AnalysisPrompt[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)

  // Load curated analysis prompts once on mount
  useEffect(() => {
    fetch('/api/analysis-prompts', { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.prompts) setAnalysisPrompts(data.prompts) })
      .catch(() => {})
  }, [])

  // Clear conversation when the active strategy changes
  useEffect(() => {
    setMessages([])
    setInput('')
  }, [strategyId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const sendQuestion = useCallback(async () => {
    const q = input.trim()
    if (!q || busy || !strategyId) return
    setInput('')
    setBusy(true)

    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(prev => [
      ...prev,
      { role: 'user', content: q },
      { role: 'assistant', content: '', streaming: true },
    ])

    try {
      const response = await fetch('/api/analyze-execution', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          strategy_id: strategyId,
          question: q,
          history,
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
            } else if (evt.type === 'error') {
              setMessages(prev => {
                const next = [...prev]
                const last = next[next.length - 1]
                if (last?.role === 'assistant') {
                  next[next.length - 1] = { ...last, content: `⚠ ${evt.message}` }
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
  }, [input, busy, messages, strategyId, modelSettings])

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      sendQuestion()
    }
  }, [sendQuestion])

  const firePreset = useCallback((question: string) => {
    if (busy || !strategyId || !question) return
    setInput(question)
    // Defer one tick so setInput flushes before sendQuestion reads it
    setTimeout(() => {
      setInput('')
      setBusy(true)
      const history = messages.map(m => ({ role: m.role, content: m.content }))
      setMessages(prev => [
        ...prev,
        { role: 'user', content: question },
        { role: 'assistant', content: '', streaming: true },
      ])
      fetch('/api/analyze-execution', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ strategy_id: strategyId, question, history, model: modelSettings.generateModel }),
      }).then(async response => {
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
                  if (last?.role === 'assistant') next[next.length - 1] = { ...last, content: last.content + evt.delta }
                  return next
                })
              } else if (evt.type === 'error') {
                setMessages(prev => {
                  const next = [...prev]
                  const last = next[next.length - 1]
                  if (last?.role === 'assistant') next[next.length - 1] = { ...last, content: `⚠ ${evt.message}` }
                  return next
                })
              }
            } catch { /* ignore */ }
          }
        }
      }).catch(err => {
        setMessages(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') next[next.length - 1] = { ...last, content: `Error: ${err}` }
          return next
        })
      }).finally(() => {
        setMessages(prev => {
          const next = [...prev]
          const last = next[next.length - 1]
          if (last?.role === 'assistant') next[next.length - 1] = { ...last, streaming: false }
          return next
        })
        setBusy(false)
      })
    }, 0)
  }, [busy, strategyId, messages, modelSettings])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{ padding: '8px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-dim)', fontSize: 11, fontFamily: 'var(--font-mono)', letterSpacing: '0.1em', display: 'flex', alignItems: 'center', gap: 8 }}>
        RUN Q&amp;A
        {strategyId && (
          <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>— {strategyId}</span>
        )}
        {messages.length > 0 && (
          <button onClick={() => setMessages([])} style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--text-muted)', background: 'transparent', border: 'none', cursor: 'pointer' }}>
            clear
          </button>
        )}
      </div>

      {/* Preset analysis buttons */}
      {strategyId && analysisPrompts.length > 0 && (
        <div style={{ padding: '6px 10px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {analysisPrompts.map(p => (
            <button
              key={p.id}
              onClick={() => firePreset(p.question)}
              disabled={busy}
              style={{
                fontSize: 10, fontFamily: 'var(--font-mono)', letterSpacing: '0.05em',
                padding: '3px 8px', borderRadius: 3, border: '1px solid var(--border)',
                background: 'var(--surface)', color: busy ? 'var(--text-muted)' : 'var(--text-dim)',
                cursor: busy ? 'default' : 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              {p.label}
            </button>
          ))}
        </div>
      )}

      {/* Messages */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        {!strategyId ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic' }}>
            No active run — submit a strategy to enable run analysis.
          </div>
        ) : messages.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic' }}>
            Ask anything about this run — fills received, signal values, why no orders, P&amp;L breakdown, etc.
          </div>
        ) : null}
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
          placeholder={strategyId ? 'Ask about the run… (⌘↵ to send)' : 'No active run'}
          rows={2}
          disabled={busy || !strategyId}
          style={{
            flex: 1, fontSize: 12, lineHeight: 1.5, resize: 'none',
            color: 'var(--text)', background: 'var(--surface)',
            border: '1px solid var(--border)', borderRadius: 4, padding: '5px 8px',
          }}
        />
        <button
          onClick={sendQuestion}
          disabled={!input.trim() || busy || !strategyId}
          style={{
            padding: '0 12px', borderRadius: 4, fontSize: 12, fontWeight: 600, border: 'none',
            background: input.trim() && !busy && strategyId ? 'var(--accent)' : 'var(--surface)',
            color: input.trim() && !busy && strategyId ? '#fff' : 'var(--text-muted)',
            cursor: input.trim() && !busy && strategyId ? 'pointer' : 'default',
          }}
        >
          Ask
        </button>
      </div>
    </div>
  )
}
