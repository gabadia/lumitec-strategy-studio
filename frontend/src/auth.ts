export interface Trader {
  traderId: string
  orgId: string
}

export function getTrader(): Trader | null {
  try {
    const raw = localStorage.getItem('lumitec_trader')
    return raw ? (JSON.parse(raw) as Trader) : null
  } catch {
    return null
  }
}

export function setTrader(trader: Trader): void {
  localStorage.setItem('lumitec_trader', JSON.stringify(trader))
}

export function clearTrader(): void {
  localStorage.removeItem('lumitec_trader')
}

export function getTraderHeaders(): Record<string, string> {
  const t = getTrader()
  if (!t) return {}
  return { 'X-Trader-Id': t.traderId, 'X-Org-Id': t.orgId }
}
