import { create } from 'zustand'
import { loadSession, redirectToLogin, redirectToLogout, completeLogin, getValidIdToken, type CognitoSession } from './cognito'

interface SessionState {
  status: 'checking' | 'authenticated' | 'anonymous'
  email: string | null
  sub: string | null
  login: () => void
  logout: () => void
  hydrate: () => void
  completeLoginWithCode: (code: string) => Promise<void>
  /** Refresh the ID token if it's near/past expiry. If the refresh token
   * itself is gone (expired after 30 days, or revoked), bounce to login
   * instead of leaving the app silently 401ing on every request. */
  refreshIfNeeded: () => Promise<void>
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

  refreshIfNeeded: async () => {
    const token = await getValidIdToken()
    if (!token) {
      // getValidIdToken() already cleared sessionStorage if a refresh was
      // attempted and failed — just sync our state and send the user back
      // through Hosted UI rather than leaving every request 401ing silently.
      set({ status: 'anonymous', email: null, sub: null })
      void redirectToLogin()
    }
  },
}))
