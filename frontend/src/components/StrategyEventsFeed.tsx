import { useEffect, useRef } from 'react'
import { useStore } from '../App'
import type { StrategyRawEvent } from '../types'

function formatTime(ts: number) {
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// Classify event type to a color category
function eventColor(eventType: string): { bg: string; text: string; border: string } {
  if (eventType.startsWith('order.') || eventType === 'order_submitted' || eventType === 'order_acknowledged' || eventType === 'order_rejected' || eventType === 'order_cancelled') {
    return { bg: '#0a1a2a', text: '#4fc3f7', border: '#1a3a5a' }
  }
  if (eventType.startsWith('fill.') || eventType === 'fill' || eventType === 'partial_fill') {
    return { bg: '#0a2a0a', text: '#81c784', border: '#1a4a1a' }
  }
  if (eventType.startsWith('strategy.')) {
    return { bg: '#1a1a0a', text: '#ffd54f', border: '#3a3a1a' }
  }
  if (eventType.includes('observe') || eventType.includes('quote') || eventType.includes('market')) {
    return { bg: '#0a0a1a', text: '#ce93d8', border: '#1a1a3a' }
  }
  if (eventType.includes('decide') || eventType.includes('signal')) {
    return { bg: '#1a0a1a', text: '#f48fb1', border: '#3a1a3a' }
  }
  return { bg: 'transparent', text: 'var(--text-dim)', border: 'transparent' }
}

function extractKeyFields(raw: Record<string, unknown>): string {
  const data = (raw.data ?? raw) as Record<string, unknown>
  const eventType = String(raw.type ?? raw.event_type ?? '')

  // Reasoning events: show message + context from nested reasoning object
  if (eventType.startsWith('strategy.reasoning.')) {
    const reasoning = data.reasoning as Record<string, unknown> | undefined
    if (reasoning) {
      const msg = reasoning.message != null ? String(reasoning.message) : ''
      const ctx = reasoning.context != null
        ? (typeof reasoning.context === 'string' ? reasoning.context : JSON.stringify(reasoning.context))
        : ''
      const parts: string[] = []
      if (msg) parts.push(msg)
      if (ctx) {
        const truncated = ctx.length > 120 ? ctx.slice(0, 120) + '…' : ctx
        parts.push(truncated)
      }
      return parts.join('  |  ')
    }
  }

  const skip = new Set(['event_type', 'type', 'strategy_id', 'supervisor_id', 'timestamp', 'termination_type'])
  const entries = Object.entries(data)
    .filter(([k]) => !skip.has(k))
    .slice(0, 6)
  if (!entries.length) return ''
  return entries.map(([k, v]) => {
    const val = typeof v === 'object' ? JSON.stringify(v) : String(v)
    const truncated = val.length > 80 ? val.slice(0, 80) + '…' : val
    return `${k}: ${truncated}`
  }).join('  |  ')
}

function EventRow({ event }: { event: StrategyRawEvent }) {
  const colors = eventColor(event.eventType)
  const isTerminal = Boolean(event.terminationType)
  const fields = extractKeyFields(event.raw)

  return (
    <div style={{
      padding: '5px 12px',
      borderBottom: '1px solid var(--border)',
      background: isTerminal ? '#1a1000' : colors.bg,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={{ color: 'var(--text-muted)', fontSize: 10, fontFamily: 'var(--font-mono)', flexShrink: 0 }}>
          {formatTime(event.timestamp)}
        </span>
        <span style={{
          fontSize: 10, fontFamily: 'var(--font-mono)', fontWeight: 600,
          padding: '1px 5px', borderRadius: 3,
          background: colors.bg, color: colors.text, border: `1px solid ${colors.border}`,
          flexShrink: 0,
        }}>
          {event.eventType}
        </span>
        {event.terminationType && (
          <span style={{
            fontSize: 10, fontFamily: 'var(--font-mono)', fontWeight: 700,
            padding: '1px 5px', borderRadius: 3,
            background: '#3a2a00', color: '#ffd54f', border: '1px solid #5a4a00',
          }}>
            {event.terminationType}
          </span>
        )}
      </div>
      {fields && (
        <div style={{
          marginTop: 2, fontSize: 10, color: 'var(--text-dim)',
          fontFamily: 'var(--font-mono)', lineHeight: 1.5, wordBreak: 'break-all',
        }}>
          {fields}
        </div>
      )}
    </div>
  )
}

export default function StrategyEventsFeed() {
  const strategyEvents = useStore((s) => s.strategyEvents)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [strategyEvents.length])

  if (!strategyEvents.length) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
        Strategy events will appear here after submission
      </div>
    )
  }

  return (
    <div style={{ flex: 1, overflowY: 'auto' }}>
      {strategyEvents.map((event) => (
        <EventRow key={event.id} event={event} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
