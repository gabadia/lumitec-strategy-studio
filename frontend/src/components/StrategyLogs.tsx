import { useState, useEffect, useRef, useCallback } from 'react'
import { authHeaders } from '../auth/cognito'

const ANSI_RE = /\x1b\[[0-9;]*m/g
const SINCE_OPTIONS = ['15m', '1h', '4h', '24h'] as const
type Since = typeof SINCE_OPTIONS[number]

interface Props {
  strategyId: string | null
  supervisorId: string | null
}

export default function StrategyLogs({ strategyId, supervisorId }: Props) {
  const [lines, setLines] = useState<string[]>([])
  const [since, setSince] = useState<Since>('1h')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [total, setTotal] = useState<number | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const fetchLogs = useCallback(async (sinceVal: Since) => {
    if (!strategyId || !supervisorId) return
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl

    setLoading(true)
    setError(null)
    setLines([])
    setTotal(null)

    try {
      const r = await fetch(
        `/api/strategies/${encodeURIComponent(strategyId)}/logs?since=${sinceVal}&n=200&supervisor_id=${encodeURIComponent(supervisorId)}`,
        { headers: authHeaders(), signal: ctrl.signal }
      )

      if (!r.ok) {
        // Try to surface the detail from the API response body
        let detail: string | null = null
        try {
          const body = await r.json() as { detail?: string }
          detail = body?.detail ?? null
        } catch {
          detail = null
        }
        const fallbacks: Record<number, string> = {
          404: 'Strategy or supervisor not found.',
          500: 'Logs not available in this environment.',
          502: 'Logs unavailable (Docker API error).',
          504: 'Logs timed out — try a shorter window.',
        }
        setError(detail ?? fallbacks[r.status] ?? `Unexpected error (${r.status}).`)
        return
      }

      const data = await r.json() as { lines: string[]; total: number }
      setLines(data.lines.map(l => l.replace(ANSI_RE, '')))
      setTotal(data.total)
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setError('Failed to fetch logs.')
      }
    } finally {
      setLoading(false)
    }
  }, [strategyId, supervisorId])

  // Fetch on mount / when strategyId or since changes
  useEffect(() => {
    fetchLogs(since)
    return () => abortRef.current?.abort()
  }, [fetchLogs, since])

  // Scroll to bottom when lines arrive
  useEffect(() => {
    bottomRef.current?.scrollIntoView()
  }, [lines])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header toolbar */}
      <div style={{
        padding: '6px 12px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0,
      }}>
        <span style={{ color: 'var(--text-dim)', fontSize: 11, fontFamily: 'var(--font-mono)', letterSpacing: '0.1em' }}>
          LOGS
        </span>
        {strategyId && (
          <span style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
            — {strategyId}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {/* Since selector */}
        <div style={{ display: 'flex', gap: 2 }}>
          {SINCE_OPTIONS.map(opt => (
            <button
              key={opt}
              onClick={() => setSince(opt)}
              disabled={loading}
              style={{
                padding: '2px 7px', fontSize: 10, fontFamily: 'var(--font-mono)',
                border: '1px solid var(--border)', borderRadius: 3, cursor: 'pointer',
                background: since === opt ? 'var(--accent)' : 'var(--surface-2)',
                color: since === opt ? '#000' : 'var(--text-dim)',
                opacity: loading ? 0.5 : 1,
              }}
            >
              {opt}
            </button>
          ))}
        </div>
        <button
          onClick={() => fetchLogs(since)}
          disabled={loading || !strategyId}
          style={{
            padding: '2px 10px', fontSize: 10, fontFamily: 'var(--font-mono)',
            border: '1px solid var(--border)', borderRadius: 3, cursor: 'pointer',
            background: 'var(--surface-2)', color: 'var(--text-dim)',
            opacity: loading || !strategyId ? 0.5 : 1,
          }}
        >
          {loading ? '…' : '↻ Refresh'}
        </button>
        {total !== null && !loading && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            {total} lines
          </span>
        )}
      </div>

      {/* Log body */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 12px' }}>
        {!strategyId ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic' }}>
            No active strategy — submit a strategy to view logs.
          </div>
        ) : loading ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
            Loading…
          </div>
        ) : error ? (
          <div style={{ color: 'var(--red)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
            {error}
          </div>
        ) : lines.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 11, fontStyle: 'italic' }}>
            No log lines found for the selected window.
          </div>
        ) : (
          lines.map((line, i) => (
            <div
              key={i}
              style={{
                fontSize: 11, fontFamily: 'var(--font-mono)', lineHeight: 1.5,
                color: line.toLowerCase().includes('error') || line.toLowerCase().includes('fail')
                  ? 'var(--red)'
                  : line.toLowerCase().includes('warn')
                  ? 'var(--yellow, #e5b400)'
                  : 'var(--text-dim)',
                whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              }}
            >
              {line}
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
