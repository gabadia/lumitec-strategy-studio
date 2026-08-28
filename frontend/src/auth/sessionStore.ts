import { create } from 'zustand'
import { loadSession, redirectToLogin, redirectToLogout, completeLogin, type CognitoSession } from './cognito'

interface SessionState {
  status: 'checking' | 'authenticated' | 'anonymous'
  email: string | null
  sub: string | null
  login: () => void
  logout: () => void
  hydrate: () => void
  completeLoginWithCode: (code: string) => Promise<void>
}

function applySession(session: CognitoSession | null): Pick<SessionState, 'status' | 'email' | 'sub'> {
  if (!session) return { status: 'anonymous', email: null, sub: null }
  return { status: 'authenticated', email: session.email, sub: session.sub }
}

export const useSessionStore = create<SessionState>((set) => ({
  status: 'checking',
  email: null,
  sub: null,

  login: () => {
    void redirectToLogin()
  },

  logout: () => {
    redirectToLogout()
  },

  hydrate: () => {
    set(applySession(loadSession()))
  },

  completeLoginWithCode: async (code: string) => {
    const session = await completeLogin(code)
    set(applySession(session))
  },
}))
