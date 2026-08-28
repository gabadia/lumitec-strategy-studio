import { useEffect, useState } from 'react'
import { useSessionStore } from '../auth/sessionStore'

interface Props {
  children: React.ReactNode
}

const centerStyle: React.CSSProperties = {
  height: '100vh',
  width: '100vw',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--bg)',
}

/**
 * Gates the app behind Cognito Hosted UI login. On mount:
 *  - if the URL carries ?code=... (Hosted UI redirect), exchanges it for
 *    tokens and cleans the URL
 *  - otherwise hydrates from any existing session in sessionStorage
 * Renders children only once authenticated; otherwise a login screen.
 */
export default function LoginGate({ children }: Props) {
  const status = useSessionStore((s) => s.status)
  const login = useSessionStore((s) => s.login)
  const hydrate = useSessionStore((s) => s.hydrate)
  const completeLoginWithCode = useSessionStore((s) => s.completeLoginWithCode)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    if (code) {
      completeLoginWithCode(code)
        .then(() => {
          window.history.replaceState({}, '', window.location.pathname)
        })
        .catch((e) => {
          setError(e instanceof Error ? e.message : String(e))
        })
      return
    }
    hydrate()
  }, [])

  if (status === 'checking') {
    return (
      <div style={centerStyle}>
        <div style={{ color: 'var(--text-dim)', fontSize: 13 }}>Signing in…</div>
      </div>
    )
  }

  if (status === 'anonymous') {
    return (
      <div style={centerStyle}>
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            padding: '32px 36px',
            display: 'flex',
            flexDirection: 'column',
            gap: 20,
            alignItems: 'center',
            minWidth: 320,
          }}
        >
          <div style={{ textAlign: 'center' }}>
            <div style={{ color: 'var(--accent)', fontWeight: 700, fontSize: 16, letterSpacing: '0.05em' }}>
              LUMITEC
            </div>
            <div style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 2 }}>Strategy Studio</div>
          </div>

          {error && <div style={{ color: '#e5484d', fontSize: 12 }}>{error}</div>}

          <button
            onClick={login}
            style={{
              padding: '8px 24px',
              background: 'var(--accent)',
              color: '#fff',
              border: 'none',
              borderRadius: 4,
              fontWeight: 600,
              fontSize: 13,
              cursor: 'pointer',
            }}
          >
            Log in
          </button>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
