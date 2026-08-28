import { useState, useCallback, useEffect, useRef, KeyboardEvent } from 'react'
import { authHeaders } from '../auth/cognito'
import { useStore } from '../App'
import type { ModelSettings } from '../App'

interface StrategyEntry { name: string; source: 'private' | 'shared' }

const AVAILABLE_MODELS = [
  { value: 'claude-opus-4-6',          label: 'Claude Opus 4.6' },
  { value: 'claude-sonnet-4-6',        label: 'Claude Sonnet 4.6' },
  { value: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5' },
  { value: 'gpt-4o',                   label: 'GPT-4o' },
  { value: 'gpt-4o-mini',              label: 'GPT-4o-mini' },
]

interface LegParam { leg_id: string; symbol: string; quantity: number; side: string; tif: string }
interface ConfigParam { name: string; type: 'int' | 'float' | 'bool' | 'str'; value: string | number | boolean }

// Real supervisors this deployment's orchestrator knows about. Whether the
// logged-in user is actually entitled to submit against one is enforced
// server-side (backend/main.py's resubmit-strategy route) — this is just
// the picker's option list.
const SUPERVISORS = ['USA-1', 'SPAIN-1']

interface Props {
  onRun: (intent: string, strategyName?: string, existingCode?: string, workflowMode?: string) => void
  onLoad: (strategyName: string) => void
  onStop: () => void
  onResubmit: (legs: object[], strategyParams: Record<string, unknown>, code: string, supervisorId: string, startTime: string, endTime: string) => void
  isRunning: boolean
  editorCode: string   // current code in the editor — used by CODE mode
  modelSettings: ModelSettings
  onModelSettingsChange: (s: ModelSettings) => void
}

const EXAMPLES = [
  'Build an AAPL momentum strategy that buys on strong upward price slope and exits on reversal. Risk limit $500.',
  'Create a TSLA/MSFT pairs trading strategy using z-score mean reversion with entry at z=2.0 and exit at z=0.5.',
  'Build a bid-ask spread capture strategy for MSFT with max inventory of 500 shares.',
]

type Mode = 'prompt' | 'code' | 'existing'

const MODE_LABELS: Record<Mode, string> = {
  prompt: 'PROMPT',
  code: 'CODE',
  existing: 'EXISTING',
}

function StopButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 16px',
        background: 'var(--red)',
        color: '#fff',
        border: 'none',
        borderRadius: 4,
        fontWeight: 600,
        fontSize: 12,
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      Stop
    </button>
  )
}

function ActionButton({ label, onClick, enabled }: { label: string; onClick: () => void; enabled: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={!enabled}
      style={{
        padding: '6px 16px',
        background: enabled ? 'var(--accent)' : 'var(--surface)',
        color: enabled ? '#fff' : 'var(--text-dim)',
        border: 'none',
        borderRadius: 4,
        fontWeight: 600,
        fontSize: 12,
        cursor: enabled ? 'pointer' : 'not-allowed',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </button>
  )
}

export default function IntentInput({ onRun, onLoad, onStop, onResubmit, isRunning, editorCode, modelSettings, onModelSettingsChange }: Props) {
  const [intent, setIntent] = useState('')
  const [mode, setMode] = useState<Mode>('prompt')
  const [strategies, setStrategies] = useState<StrategyEntry[]>([])
  const [selected, setSelected] = useState<string>('')
  const [codeName, setCodeName] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [showModels, setShowModels] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [showFeedback, setShowFeedback] = useState(false)
  const [showChangeSubmission, setShowChangeSubmission] = useState(false)
  const [showFixCode, setShowFixCode] = useState(false)
  const [legs, setLegs] = useState<LegParam[]>([])
  const [params, setParams] = useState<ConfigParam[]>([])
  const [parseBusy, setParseBusy] = useState(false)
  const [startTime, setStartTime] = useState('')
  const [endTime, setEndTime] = useState('')
  const [supervisorId, setSupervisorId] = useState(SUPERVISORS[0])
  const [isFirstSubmit, setIsFirstSubmit] = useState(false)
  const prevRunningRef = useRef(false)

  const setCode = useStore((s) => s.setCode)
  const pendingSubmission = useStore((s) => s.pendingSubmission)
  const setPendingSubmission = useStore((s) => s.setPendingSubmission)

  // Show post-run panels when a run transitions from running → stopped
  useEffect(() => {
    if (prevRunningRef.current && !isRunning) {
      setShowFeedback(true)
    }
    prevRunningRef.current = isRunning
  }, [isRunning])

  // When the backend emits params_ready after generation+validation,
  // auto-open the Change Submission form pre-populated — user must explicitly submit
  useEffect(() => {
    if (!pendingSubmission) return
    setShowFixCode(false)
    // Default times: NYSE session (09:30–16:00 ET)
    const toLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
    const etDateStr = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' })
    const etOffsetStr = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', timeZoneName: 'shortOffset' })
      .formatToParts(new Date()).find(p => p.type === 'timeZoneName')?.value ?? 'GMT-4'
    const etOffsetHours = parseInt(etOffsetStr.replace('GMT', '') || '-4')
    const [y, mo, d] = etDateStr.split('-').map(Number)
    const openET  = new Date(Date.UTC(y, mo - 1, d,  9 - etOffsetHours, 30))
    const closeET = new Date(Date.UTC(y, mo - 1, d, 16 - etOffsetHours,  0))
    setStartTime(toLocal(openET))
    setEndTime(toLocal(closeET))
    setLegs(pendingSubmission.legs as LegParam[])
    setParams(pendingSubmission.params as ConfigParam[])
    setIsFirstSubmit(pendingSubmission.isFirstSubmit)
    setShowChangeSubmission(true)
    setPendingSubmission(null)
  }, [pendingSubmission, setPendingSubmission])

  const openChangeSubmission = useCallback(async () => {
    if (!editorCode.trim() || parseBusy) return
    setParseBusy(true)
    setIsFirstSubmit(false)
    setShowFeedback(false)
    setShowFixCode(false)
    // Default times: NYSE session (09:30–16:00 ET) in local browser time
    const toLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
    const etDateStr = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' }) // YYYY-MM-DD
    const etOffsetStr = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', timeZoneName: 'shortOffset' })
      .formatToParts(new Date()).find(p => p.type === 'timeZoneName')?.value ?? 'GMT-4'
    const etOffsetHours = parseInt(etOffsetStr.replace('GMT', '') || '-4') // e.g. -4 for EDT
    const [y, mo, d] = etDateStr.split('-').map(Number)
    const openET  = new Date(Date.UTC(y, mo - 1, d,  9 - etOffsetHours, 30))
    const closeET = new Date(Date.UTC(y, mo - 1, d, 16 - etOffsetHours,  0))
    setStartTime(toLocal(openET))
    setEndTime(toLocal(closeET))
    try {
      const r = await fetch('/api/parse-strategy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ code: editorCode }),
      })
      const d = await r.json()
      setLegs(d.legs ?? [])
      setParams(d.params ?? [])
      setShowChangeSubmission(true)
    } catch (err) {
      console.error('[parse-strategy] failed:', err)
    } finally { setParseBusy(false) }
  }, [editorCode, parseBusy])

  const handleResubmit = useCallback(() => {
    const strategyParams: Record<string, unknown> = {}
    for (const p of params) strategyParams[p.name] = p.value
    // Convert datetime-local values (local browser time) to ISO 8601 UTC
    const toISO = (local: string) => local ? new Date(local).toISOString() : ''
    setShowChangeSubmission(false)
    setShowFeedback(false)
    onResubmit(legs, strategyParams, editorCode, supervisorId, toISO(startTime), toISO(endTime))
  }, [legs, params, editorCode, supervisorId, startTime, endTime, onResubmit])

  useEffect(() => {
    if (mode !== 'existing' || strategies.length > 0) return
    fetch('/api/strategies', { headers: authHeaders() })
      .then((r) => r.json())
      .then((d) => setStrategies(d.strategies ?? []))
      .catch(() => {})
  }, [mode, strategies.length])

  const handleLoad = useCallback(async () => {
    if (!selected || isRunning || busy) return
    setBusy(true)
    try {
      onLoad(selected)
      // Loading isn't a "run" (isRunning never flips), so the normal
      // running→stopped effect that reveals the post-load action bar never
      // fires — surface it directly so a loaded strategy can be submitted.
      setShowFeedback(true)
    } finally { setBusy(false) }
  }, [selected, isRunning, busy, onLoad])

  const retryWithFeedback = useCallback(() => {
    if (!feedback.trim() || isRunning || busy) return
    // Pass the current editor code as existingCode so Phase 1 reviews and fixes
    // the specific issue rather than regenerating from scratch.
    const fixIntent =
      (intent.trim() ? intent.trim() + '\n\n' : '') +
      `Runtime error to fix:\n${feedback.trim()}`
    setFeedback('')
    setShowFeedback(false)
    setShowFixCode(false)
    onRun(fixIntent, undefined, editorCode, 'fast')
  }, [feedback, intent, editorCode, isRunning, busy, onRun])

  const runWithMode = useCallback(async (workflowMode: string) => {
    if (isRunning || busy) return
    setShowFeedback(false)
    setFeedback('')

    if (mode === 'existing') {
      if (!selected) return
      setBusy(true)
      try {
        const r = await fetch(`/api/strategies/${selected}`, { headers: authHeaders() })
        const d = await r.json()
        onRun(intent.trim(), selected, d.code, workflowMode)
      } finally { setBusy(false) }

    } else if (mode === 'code') {
      if (!editorCode.trim()) return
      onRun(intent.trim(), codeName.trim() || undefined, editorCode, workflowMode)

    } else {
      // prompt
      if (!intent.trim()) return
      onRun(intent.trim(), undefined, undefined, workflowMode)
    }
  }, [intent, editorCode, isRunning, busy, mode, selected, onRun])

  const onKeyDown = useCallback((e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      runWithMode('fast')
    }
  }, [runWithMode])

  const hasContent = mode === 'existing'
    ? !!selected
    : mode === 'code'
    ? !!editorCode.trim()
    : !!intent.trim()

  const canAct = hasContent && !isRunning && !busy

  return (
    <div style={{
      padding: '12px 16px',
      background: 'var(--surface)',
      borderBottom: '1px solid var(--border)',
      flexShrink: 0,
    }}>
      {/* Mode toggle + model settings toggle */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 8, alignItems: 'center' }}>
        {(['prompt', 'code', 'existing'] as Mode[]).map((m) => (
          <button
            key={m}
            onClick={() => !isRunning && setMode(m)}
            style={{
              padding: '3px 12px',
              borderRadius: 4,
              fontSize: 11,
              fontFamily: 'var(--font-mono)',
              fontWeight: 600,
              letterSpacing: '0.05em',
              background: mode === m ? 'var(--accent-dim)' : 'transparent',
              color: mode === m ? 'var(--accent)' : 'var(--text-muted)',
              border: `1px solid ${mode === m ? 'var(--accent)' : 'var(--border)'}`,
              cursor: isRunning ? 'default' : 'pointer',
            }}
          >
            {MODE_LABELS[m]}
          </button>
        ))}
        <div style={{ flex: 1 }} />
        <button
          onClick={() => setShowModels((v) => !v)}
          title="Model settings"
          style={{
            padding: '3px 10px',
            borderRadius: 4,
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
            fontWeight: 600,
            background: showModels ? 'var(--accent-dim)' : 'transparent',
            color: showModels ? 'var(--accent)' : 'var(--text-muted)',
            border: `1px solid ${showModels ? 'var(--accent)' : 'var(--border)'}`,
            cursor: 'pointer',
          }}
        >
          ⚙ Models
        </button>
      </div>

      {/* Model settings row */}
      {showModels && (
        <div style={{
          display: 'flex', gap: 16, alignItems: 'center',
          marginBottom: 8, padding: '6px 10px',
          background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 6,
          flexWrap: 'wrap',
        }}>
          {([
            { key: 'generateModel', label: 'Generate' },
            { key: 'validateModel', label: 'Validate' },
            { key: 'monitorModel',  label: 'Monitor'  },
          ] as { key: keyof ModelSettings; label: string }[]).map(({ key, label }) => (
            <label key={key} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-dim)' }}>
              <span style={{ fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' }}>{label}</span>
              <select
                value={modelSettings[key]}
                onChange={(e) => onModelSettingsChange({ ...modelSettings, [key]: e.target.value })}
                disabled={isRunning}
                style={{
                  background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4,
                  color: 'var(--text)', padding: '2px 6px', fontSize: 11,
                  fontFamily: 'var(--font-mono)', cursor: 'pointer',
                }}
              >
                {AVAILABLE_MODELS.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </label>
          ))}

          {/* Validation profile selector */}
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-dim)', marginLeft: 8, paddingLeft: 8, borderLeft: '1px solid var(--border)' }}>
            <span style={{ fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' }}>Profile</span>
            <select
              value={modelSettings.validationProfile}
              onChange={(e) => onModelSettingsChange({ ...modelSettings, validationProfile: e.target.value })}
              disabled={isRunning}
              style={{
                background: modelSettings.validationProfile !== 'prod' ? 'var(--surface-3, #2a2a1a)' : 'var(--surface)',
                border: `1px solid ${modelSettings.validationProfile !== 'prod' ? 'var(--warn, #b8860b)' : 'var(--border)'}`,
                borderRadius: 4,
                color: modelSettings.validationProfile !== 'prod' ? 'var(--warn, #d4a017)' : 'var(--text)',
                padding: '2px 6px', fontSize: 11,
                fontFamily: 'var(--font-mono)', cursor: 'pointer',
              }}
            >
              <option value="prod">prod</option>
              <option value="dev">dev</option>
              <option value="research">research</option>
            </select>
          </label>
        </div>
      )}

      <div style={{
        display: 'flex',
        gap: 8,
        background: 'var(--surface-2)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '8px 10px',
      }}>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>

          {/* EXISTING: dropdown + buttons */}
          {mode === 'existing' && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <select
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
                disabled={isRunning}
                style={{
                  width: 'fit-content',
                  maxWidth: '100%',
                  background: 'var(--surface)',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  color: selected ? 'var(--text)' : 'var(--text-muted)',
                  padding: '5px 8px',
                  fontSize: 12,
                  fontFamily: 'var(--font-mono)',
                  cursor: 'pointer',
                }}
              >
                <option value="">— select a strategy —</option>
                {strategies.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}{s.source === 'shared' ? ' (shared)' : ''}
                  </option>
                ))}
              </select>

              {isRunning ? (
                <StopButton onClick={onStop} />
              ) : (
                <>
                  <ActionButton label={busy ? 'Loading…' : 'Load'}     onClick={handleLoad}                    enabled={!!selected && !busy} />
                  <ActionButton label="Fast Run"                         onClick={() => runWithMode('fast')} enabled={canAct} />
                  <ActionButton label="Full Run"                         onClick={() => runWithMode('full')} enabled={canAct} />
                </>
              )}
            </div>
          )}

          {/* CODE: filename input + buttons */}
          {mode === 'code' && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input
                type="text"
                value={codeName}
                onChange={(e) => setCodeName(e.target.value)}
                disabled={isRunning}
                placeholder="Strategy name (optional)"
                style={{
                  background: 'var(--surface)',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  color: 'var(--text)',
                  padding: '5px 8px',
                  fontSize: 12,
                  fontFamily: 'var(--font-mono)',
                  width: 220,
                }}
              />
              {isRunning ? (
                <StopButton onClick={onStop} />
              ) : (
                <>
                  <ActionButton label="Fast Run" onClick={() => runWithMode('fast')} enabled={canAct} />
                  <ActionButton label="Full Run" onClick={() => runWithMode('full')} enabled={canAct} />
                </>
              )}
            </div>
          )}

          {/* Code paste area — CODE mode only, when editor is empty */}
          {mode === 'code' && !editorCode.trim() && (
            <textarea
              placeholder="Paste your Python strategy code here…"
              onChange={(e) => { if (e.target.value) setCode(e.target.value) }}
              disabled={isRunning}
              rows={4}
              style={{
                flex: 1,
                color: isRunning ? 'var(--text-dim)' : 'var(--text)',
                lineHeight: 1.6,
                fontFamily: 'var(--font-mono)',
                fontSize: 11,
              }}
            />
          )}

          {/* Intent textarea — prompt mode always, existing/code modes for notes */}
          {(mode === 'prompt' || mode === 'existing' || (mode === 'code' && !!editorCode.trim())) && (
            <textarea
              value={intent}
              onChange={(e) => setIntent(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={
                mode === 'prompt'
                  ? 'Describe your trading strategy… (⌘↵ to run)'
                  : 'Optional: notes or adjustments for this run…'
              }
              rows={mode === 'prompt' ? 2 : 1}
              disabled={isRunning}
              style={{
                flex: 1,
                color: isRunning ? 'var(--text-dim)' : 'var(--text)',
                lineHeight: 1.6,
              }}
            />
          )}
        </div>

        {/* Action buttons — PROMPT mode only */}
        {mode === 'prompt' && (
          <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 6 }}>
            {isRunning ? (
              <StopButton onClick={onStop} />
            ) : (
              <>
                <ActionButton label="Fast ⌘↵" onClick={() => runWithMode('fast')} enabled={canAct} />
                <ActionButton label="Full Run" onClick={() => runWithMode('full')} enabled={canAct} />
              </>
            )}
          </div>
        )}
      </div>

      {/* Post-run action bar — shown after any run stops */}
      {showFeedback && !isRunning && !showChangeSubmission && (
        <div style={{
          marginTop: 8, padding: '8px 10px',
          background: 'var(--surface-2)', border: '1px solid var(--accent)',
          borderRadius: 6, display: 'flex', flexDirection: 'column', gap: 6,
        }}>
          <div style={{ fontSize: 11, color: 'var(--accent)', fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' }}>
            WHAT WOULD YOU LIKE TO DO?
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <button
              onClick={openChangeSubmission}
              disabled={!editorCode.trim() || parseBusy}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: '1px solid var(--accent)', background: 'transparent', color: 'var(--accent)', cursor: editorCode.trim() ? 'pointer' : 'not-allowed' }}
            >
              {parseBusy ? 'Loading…' : 'Change Submission'}
            </button>
            <button
              onClick={() => { setShowFeedback(false); setShowFixCode(true) }}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-dim)', cursor: 'pointer' }}
            >
              Fix Code
            </button>
            <button
              onClick={() => { setShowFeedback(false); setFeedback('') }}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-muted)', cursor: 'pointer' }}
            >
              Dismiss
            </button>
          </div>
          {/* Fix Code feedback textarea — shown when Fix Code is active */}
          {!showChangeSubmission && !showFeedback && null}
        </div>
      )}

      {/* Fix Code panel */}
      {showFixCode && !isRunning && (
        <div style={{ marginTop: 8, padding: '8px 10px', background: 'var(--surface-2)', border: '1px solid var(--accent)', borderRadius: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ fontSize: 11, color: 'var(--accent)', fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' }}>
            FIX CODE — describe the issue and the model will regenerate
          </div>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="e.g. both legs use the same symbol so warmup never completes…"
            rows={2}
            style={{ color: 'var(--text)', lineHeight: 1.6, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, padding: '6px 8px', fontSize: 12, resize: 'vertical' }}
          />
          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={retryWithFeedback} disabled={!feedback.trim()}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: 'none', cursor: feedback.trim() ? 'pointer' : 'not-allowed', background: feedback.trim() ? 'var(--accent)' : 'var(--surface)', color: feedback.trim() ? '#fff' : 'var(--text-dim)' }}>
              Fix &amp; Retry
            </button>
            <button onClick={() => { setShowFixCode(false); setShowFeedback(true); setFeedback('') }}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-dim)', cursor: 'pointer' }}>
              Back
            </button>
          </div>
        </div>
      )}

      {/* Change Submission panel */}
      {showChangeSubmission && !isRunning && (
        <div style={{
          marginTop: 8, padding: '10px 12px',
          background: 'var(--surface-2)', border: '1px solid var(--accent)',
          borderRadius: 6, display: 'flex', flexDirection: 'column', gap: 10,
          maxHeight: '62vh', overflowY: 'auto',
        }}>
          <div style={{ fontSize: 11, color: 'var(--accent)', fontFamily: 'var(--font-mono)', letterSpacing: '0.05em' }}>
            CHANGE SUBMISSION
          </div>

          {/* Legs */}
          {legs.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4, fontFamily: 'var(--font-mono)' }}>LEGS</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {legs.map((leg, i) => (
                  <div key={leg.leg_id} style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', minWidth: 24 }}>{leg.leg_id}</span>
                    <input value={leg.symbol} onChange={e => setLegs(prev => prev.map((l, j) => j === i ? { ...l, symbol: e.target.value.toUpperCase() } : l))}
                      placeholder="Symbol" style={{ width: 72, padding: '3px 6px', fontSize: 12, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)' }} />
                    <input type="number" value={leg.quantity} onChange={e => setLegs(prev => prev.map((l, j) => j === i ? { ...l, quantity: parseInt(e.target.value) || 0 } : l))}
                      style={{ width: 72, padding: '3px 6px', fontSize: 12, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)' }} />
                    <select value={leg.side} onChange={e => setLegs(prev => prev.map((l, j) => j === i ? { ...l, side: e.target.value } : l))}
                      style={{ padding: '3px 6px', fontSize: 12, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)', cursor: 'pointer' }}>
                      <option>BUY</option><option>SELL</option>
                    </select>
                    <select value={leg.tif} onChange={e => setLegs(prev => prev.map((l, j) => j === i ? { ...l, tif: e.target.value } : l))}
                      style={{ padding: '3px 6px', fontSize: 12, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)', cursor: 'pointer' }}>
                      <option>DAY</option><option>GTC</option>
                    </select>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Strategy params */}
          {params.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4, fontFamily: 'var(--font-mono)' }}>STRATEGY PARAMS</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {params.map((p, i) => (
                  <div key={p.name} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', minWidth: 160 }}>{p.name}</span>
                    {p.type === 'bool' ? (
                      <input type="checkbox" checked={!!p.value} onChange={e => setParams(prev => prev.map((q, j) => j === i ? { ...q, value: e.target.checked } : q))} />
                    ) : (
                      <input
                        type={p.type === 'int' || p.type === 'float' ? 'number' : 'text'}
                        step={p.type === 'float' ? '0.01' : '1'}
                        value={p.value as string | number}
                        onChange={e => {
                          const raw = e.target.value
                          const val = p.type === 'int' ? parseInt(raw) : p.type === 'float' ? parseFloat(raw) : raw
                          setParams(prev => prev.map((q, j) => j === i ? { ...q, value: val } : q))
                        }}
                        style={{ width: 120, padding: '3px 6px', fontSize: 12, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)' }}
                      />
                    )}
                    <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{p.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Supervisor */}
          <div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4, fontFamily: 'var(--font-mono)' }}>
              SUPERVISOR
            </div>
            <select value={supervisorId} onChange={e => setSupervisorId(e.target.value)}
              style={{ padding: '3px 6px', fontSize: 12, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)', cursor: 'pointer' }}>
              {SUPERVISORS.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>

          {/* Session timing */}
          <div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4, fontFamily: 'var(--font-mono)' }}>
              SESSION WINDOW
              <span style={{ marginLeft: 6, color: 'var(--text-muted)', fontWeight: 400 }}>
                ({new Intl.DateTimeFormat('en-US', { timeZoneName: 'short' }).formatToParts(new Date()).find(p => p.type === 'timeZoneName')?.value ?? 'local'})
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <label style={{ fontSize: 11, color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 6 }}>
                Start
                <input type="datetime-local" value={startTime} onChange={e => setStartTime(e.target.value)}
                  style={{ padding: '3px 6px', fontSize: 11, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)' }} />
              </label>
              <label style={{ fontSize: 11, color: 'var(--text-dim)', display: 'flex', alignItems: 'center', gap: 6 }}>
                End
                <input type="datetime-local" value={endTime} onChange={e => setEndTime(e.target.value)}
                  style={{ padding: '3px 6px', fontSize: 11, fontFamily: 'var(--font-mono)', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text)' }} />
              </label>
            </div>
          </div>

          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={handleResubmit}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: 'none', background: 'var(--accent)', color: '#fff', cursor: 'pointer' }}>
              {isFirstSubmit ? 'Submit' : 'Resubmit'}
            </button>
            <button onClick={() => { setShowChangeSubmission(false); setShowFeedback(true) }}
              style={{ padding: '5px 14px', borderRadius: 4, fontWeight: 600, fontSize: 12, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-dim)', cursor: 'pointer' }}>
              Back
            </button>
          </div>
        </div>
      )}

      {/* Example prompts (prompt mode only) */}
      {mode === 'prompt' && !isRunning && !intent && (
        <div style={{ marginTop: 6, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {EXAMPLES.map((ex, i) => (
            <button
              key={i}
              onClick={() => setIntent(ex)}
              style={{
                padding: '2px 8px',
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 12,
                color: 'var(--text-dim)',
                fontSize: 11,
                cursor: 'pointer',
              }}
            >
              {ex.slice(0, 48)}…
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
