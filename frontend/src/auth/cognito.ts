// src/auth/cognito.ts
//
// Cognito Hosted UI login via Authorization Code + PKCE, against Studio's
// own dedicated app client on the same user pool lumitec-desk-ui uses (see
// lumitec-desk-cloud/terraform/modules/cognito/main.tf). Ported near-literally
// from lumitec-desk-ui/src/auth/cognito.ts — same mechanics, same pool.
// No auth library dependency: this is a small, self-contained PKCE client.

const DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN ?? ''
const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID ?? ''
const REDIRECT_URI = import.meta.env.VITE_COGNITO_REDIRECT_URI ?? `${window.location.origin}/callback`
const LOGOUT_URI = import.meta.env.VITE_COGNITO_LOGOUT_URI ?? window.location.origin
const SCOPES = 'openid email profile'

const STORAGE_KEY = 'cognito:session'
const VERIFIER_KEY = 'cognito:pkce_verifier'

export interface CognitoSession {
  idToken: string
  accessToken: string
  refreshToken: string
  expiresAt: number // epoch ms
  email: string | null
  sub: string | null
  groups: string[]
  organizationId: string | null
}

function base64UrlEncode(bytes: Uint8Array): string {
  let str = ''
  for (const b of bytes) str += String.fromCharCode(b)
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function sha256(input: string): Promise<Uint8Array> {
  const data = new TextEncoder().encode(input)
  const digest = await crypto.subtle.digest('SHA-256', data)
  return new Uint8Array(digest)
}

function randomVerifier(): string {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  return base64UrlEncode(bytes)
}

export function decodeJwt(token: string): Record<string, any> {
  const payload = token.split('.')[1]
  const padded = payload.replace(/-/g, '+').replace(/_/g, '/')
  const json = decodeURIComponent(
    atob(padded)
      .split('')
      .map((c) => '%' + c.charCodeAt(0).toString(16).padStart(2, '0'))
      .join('')
  )
  return JSON.parse(json)
}

function saveSession(s: CognitoSession) {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(s))
}

export function loadSession(): CognitoSession | null {
  const raw = sessionStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as CognitoSession
  } catch {
    return null
  }
}

export function clearSession() {
  sessionStorage.removeItem(STORAGE_KEY)
  sessionStorage.removeItem(VERIFIER_KEY)
}

/** Redirect the browser to the Cognito Hosted UI login page. */
export async function redirectToLogin(): Promise<void> {
  const verifier = randomVerifier()
  sessionStorage.setItem(VERIFIER_KEY, verifier)
  const challenge = base64UrlEncode(await sha256(verifier))

  const url = new URL(`https://${DOMAIN}/oauth2/authorize`)
  url.searchParams.set('client_id', CLIENT_ID)
  url.searchParams.set('response_type', 'code')
  url.searchParams.set('scope', SCOPES)
  url.searchParams.set('redirect_uri', REDIRECT_URI)
  url.searchParams.set('code_challenge_method', 'S256')
  url.searchParams.set('code_challenge', challenge)

  window.location.href = url.toString()
}

export function redirectToLogout(): void {
  clearSession()
  const url = new URL(`https://${DOMAIN}/logout`)
  url.searchParams.set('client_id', CLIENT_ID)
  url.searchParams.set('logout_uri', LOGOUT_URI)
  window.location.href = url.toString()
}

function sessionFromTokenResponse(tokens: {
  id_token: string
  access_token: string
  refresh_token?: string
  expires_in: number
}): CognitoSession {
  const claims = decodeJwt(tokens.id_token)
  const rawGroups = claims['cognito:groups']
  const groups: string[] = Array.isArray(rawGroups)
    ? rawGroups
    : typeof rawGroups === 'string' && rawGroups
    ? rawGroups.split(',').map((g) => g.trim()).filter(Boolean)
    : []
  return {
    idToken: tokens.id_token,
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token ?? loadSession()?.refreshToken ?? '',
    expiresAt: Date.now() + tokens.expires_in * 1000,
    email: claims.email ?? null,
    sub: claims.sub ?? null,
    groups,
    organizationId: claims['custom:organization_id'] ?? null,
  }
}

/** Exchange the ?code= from the Hosted UI redirect for tokens. */
export async function completeLogin(code: string): Promise<CognitoSession> {
  const verifier = sessionStorage.getItem(VERIFIER_KEY)
  if (!verifier) throw new Error('Missing PKCE verifier — login flow was not started from this browser session')

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: CLIENT_ID,
    code,
    redirect_uri: REDIRECT_URI,
    code_verifier: verifier,
  })

  const res = await fetch(`https://${DOMAIN}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`Token exchange failed: ${res.status} ${text}`)
  }
  const tokens = await res.json()
  const session = sessionFromTokenResponse(tokens)
  saveSession(session)
  sessionStorage.removeItem(VERIFIER_KEY)
  return session
}

async function refreshSession(refreshToken: string): Promise<CognitoSession> {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    client_id: CLIENT_ID,
    refresh_token: refreshToken,
  })
  const res = await fetch(`https://${DOMAIN}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!res.ok) throw new Error(`Token refresh failed: ${res.status}`)
  const tokens = await res.json()
  const session = sessionFromTokenResponse({ ...tokens, refresh_token: refreshToken })
  saveSession(session)
  return session
}

/**
 * Current, non-expired ID token — refreshes via refresh_token if needed.
 * Returns null if there's no session (caller should treat as logged out).
 */
export async function getValidIdToken(): Promise<string | null> {
  const session = loadSession()
  if (!session) return null
  const EXPIRY_SKEW_MS = 30_000
  if (Date.now() < session.expiresAt - EXPIRY_SKEW_MS) {
    return session.idToken
  }
  if (!session.refreshToken) {
    clearSession()
    return null
  }
  try {
    const refreshed = await refreshSession(session.refreshToken)
    return refreshed.idToken
  } catch {
    clearSession()
    return null
  }
}

/** Synchronous best-effort read for places that can't await (may be stale near expiry). */
export function peekIdToken(): string | null {
  return loadSession()?.idToken ?? null
}

/** Drop-in header object for fetch() calls — spread into an existing headers object. */
export function authHeaders(): Record<string, string> {
  const token = peekIdToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}
