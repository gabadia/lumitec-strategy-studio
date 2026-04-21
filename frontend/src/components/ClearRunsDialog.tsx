import { useState, useEffect, useCallback } from 'react'
import { getTraderHeaders } from '../auth'

interface RunDbEntry {
  strategy_id: string
  stored_at: string | null
  event_count: number
  size_bytes: number
}

interface Props {
  onClose: () => void
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function ClearRunsDialog({ onClose }: Props) {
  const [entries, setEntries] = useState<RunDbEntry[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [deleting, setDeleting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/run-databases', { headers: getTraderHeaders() })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setEntries(data.databases ?? [])
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const allSelected = entries.length > 0 && selected.size === entries.length

  const toggleAll = () => {
    setConfirming(false)
    if (allSelected) {
      setSelected(new Set())
    } else {
      setSelected(new Set(entries.map(e => e.strategy_id)))
    }
  }

  const toggle = (id: string) => {
    setConfirming(false)
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleDelete = async () => {
    if (selected.size === 0) return
    if (!confirming) { setConfirming(true); return }
    setConfirming(false)
    setDeleting(true)
    setError(null)
    try {
      const res = await fetch('/api/run-databases', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
        body: JSON.stringify({ strategy_ids: Array.from(selected) }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await load()
      setSelected(new Set())
    } catch (e) {
      setError(String(e))
    } finally {
      setDeleting(false)
    }
  }

  const s: Record<string, React.CSSProperties> = {
    overlay: {
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 1000,
    },
    dialog: {
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 6, width: 580, maxHeight: '70vh',
      display: 'flex', flexDirection: 'column', overflow: 'hidden',
    },
    header: {
      padding: '12px 16px', borderBottom: '1px solid var(--border)',
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    },
    title: { fontSize: 13, fontWeight: 600, color: 'var(--text)', fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' },
    closeBtn: { background: 'transparent', border: 'none', color: 'var(--text-muted)', fontSize: 16, cursor: 'pointer', lineHeight: 1 },
    tableWrap: { flex: 1, overflowY: 'auto' },
    table: { width: '100%', borderCollapse: 'collapse' as const, fontSize: 12 },
    th: { padding: '7px 12px', textAlign: 'left' as const, color: 'var(--text-muted)', fontWeight: 500, fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.08em', borderBottom: '1px solid var(--border)', background: 'var(--surface)' },
    td: { padding: '7px 12px', color: 'var(--text-dim)', borderBottom: '1px solid var(--border)', verticalAlign: 'middle' as const },
    footer: {
      padding: '10px 16px', borderTop: '1px solid var(--border)',
      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
    },
  }

  return (
    <div style={s.overlay} onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div style={s.dialog}>
        <div style={s.header}>
          <span style={s.title}>MANAGE RUN DATABASES</span>
          <button style={s.closeBtn} onClick={onClose}>✕</button>
        </div>

        <div style={s.tableWrap}>
          {loading ? (
            <div style={{ padding: 24, color: 'var(--text-muted)', fontSize: 12 }}>Loading…</div>
          ) : entries.length === 0 ? (
            <div style={{ padding: 24, color: 'var(--text-muted)', fontSize: 12, fontStyle: 'italic' }}>No run databases found.</div>
          ) : (
            <table style={s.table}>
              <thead>
                <tr>
                  <th style={{ ...s.th, width: 40 }}>
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      title={allSelected ? 'Deselect all' : 'Select all'}
                    />
                  </th>
                  <th style={s.th}>STRATEGY ID</th>
                  <th style={s.th}>STORED AT</th>
                  <th style={{ ...s.th, textAlign: 'right' as const }}>EVENTS</th>
                  <th style={{ ...s.th, textAlign: 'right' as const }}>SIZE</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(e => (
                  <tr
                    key={e.strategy_id}
                    onClick={() => toggle(e.strategy_id)}
                    style={{ cursor: 'pointer', background: selected.has(e.strategy_id) ? 'var(--surface-2)' : 'transparent' }}
                  >
                    <td style={s.td}>
                      <input
                        type="checkbox"
                        checked={selected.has(e.strategy_id)}
                        onChange={() => toggle(e.strategy_id)}
                        onClick={ev => ev.stopPropagation()}
                      />
                    </td>
                    <td style={{ ...s.td, fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text)' }}>
                      {e.strategy_id}
                    </td>
                    <td style={s.td}>{fmtDate(e.stored_at)}</td>
                    <td style={{ ...s.td, textAlign: 'right' as const }}>{e.event_count.toLocaleString()}</td>
                    <td style={{ ...s.td, textAlign: 'right' as const }}>{fmtBytes(e.size_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div style={s.footer}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {selected.size > 0
              ? `${selected.size} of ${entries.length} selected`
              : `${entries.length} database${entries.length !== 1 ? 's' : ''}`}
          </div>
          {error && <span style={{ fontSize: 11, color: 'var(--red)', flex: 1, textAlign: 'center' }}>{error}</span>}
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={onClose}
              style={{ padding: '5px 14px', borderRadius: 4, fontSize: 12, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-dim)', cursor: 'pointer' }}
            >
              Cancel
            </button>
            {confirming && (
              <button
                onClick={() => setConfirming(false)}
                style={{ padding: '5px 14px', borderRadius: 4, fontSize: 12, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-dim)', cursor: 'pointer' }}
              >
                No
              </button>
            )}
            <button
              onClick={handleDelete}
              disabled={selected.size === 0 || deleting}
              style={{
                padding: '5px 14px', borderRadius: 4, fontSize: 12, border: 'none', fontWeight: 600,
                background: selected.size > 0 && !deleting ? 'var(--red)' : 'var(--surface-2)',
                color: selected.size > 0 && !deleting ? '#fff' : 'var(--text-muted)',
                cursor: selected.size > 0 && !deleting ? 'pointer' : 'default',
              }}
            >
              {deleting ? 'Deleting…' : confirming ? `Yes, delete (${selected.size})` : `Delete${selected.size > 0 ? ` (${selected.size})` : ''}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
