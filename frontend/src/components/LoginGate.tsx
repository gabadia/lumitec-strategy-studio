import { useState } from 'react'
import { getTrader, setTrader, type Trader } from '../auth'

interface Props {
  children: React.ReactNode
}

function LoginForm({ onLogin }: { onLogin: (t: Trader) => void }) {
  const [traderId, setTraderId] = useState('')
  const [orgId, setOrgId] = useState('')

  const canSubmit = traderId.trim().length > 0 && orgId.trim().length > 0

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    const trader = { traderId: traderId.trim(), orgId: orgId.trim() }
    setTrader(trader)
    onLogin(trader)
  }

  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100vh', background: 'var(--bg)',
    }}>
      <form onSubmit={handleSubmit} style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 8,
        padding: '32px 36px',
        display: 'flex', flexDirection: 'column', gap: 20,
        minWidth: 320,
      }}>
        <div>
          <div style={{ color: 'var(--accent)', fontWeight: 700, fontSize: 16, letterSpacing: '0.05em' }}>LUMITEC</div>
          <div style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 2 }}>Strategy Studio</div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', letterSpacing: '0.08em' }}>TRADER ID</span>
            <input
              type="text"
              value={traderId}
              onChange={(e) => setTraderId(e.target.value)}
              placeholder="e.g. MEMO-DESK"
              autoFocus
              style={{
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                color: 'var(--text)',
                padding: '7px 10px',
                fontSize: 13,
                fontFamily: 'var(--font-mono)',
              }}
            />
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', letterSpacing: '0.08em' }}>ORGANISATION</span>
            <input
              type="text"
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
              placeholder="e.g. ECX_001"
              style={{
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                color: 'var(--text)',
                padding: '7px 10px',
                fontSize: 13,
                fontFamily: 'var(--font-mono)',
              }}
            />
          </label>
        </div>

        <button
          type="submit"
          disabled={!canSubmit}
          style={{
            padding: '8px 0',
            background: canSubmit ? 'var(--accent)' : 'var(--surface-2)',
            color: canSubmit ? '#fff' : 'var(--text-muted)',
            border: 'none',
            borderRadius: 4,
            fontWeight: 600,
            fontSize: 13,
            cursor: canSubmit ? 'pointer' : 'not-allowed',
          }}
        >
          Enter Studio
        </button>
      </form>
    </div>
  )
}

export default function LoginGate({ children }: Props) {
  const [trader, setTraderState] = useState<Trader | null>(getTrader)

  if (!trader) {
    return <LoginForm onLogin={(t) => setTraderState(t)} />
  }

  return <>{children}</>
}
