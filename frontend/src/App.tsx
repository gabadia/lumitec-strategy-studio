import { useCallback, useRef, useState } from 'react'
import { create } from 'zustand'
import IntentInput from './components/IntentInput'
import ActivityFeed from './components/ActivityFeed'
import StrategyEventsFeed from './components/StrategyEventsFeed'
import CodePanel from './components/CodePanel'
import RunQA from './components/RunQA'
import StrategyLogs from './components/StrategyLogs'
import StatusStepper from './components/StatusStepper'
import LoginGate from './components/LoginGate'
import ClearRunsDialog from './components/ClearRunsDialog'
import type { ActivityEntry, StrategyRawEvent, StudioEvent, WorkflowStep } from './types'
import { toolToStep } from './types'
import { clearTrader, getTrader, getTraderHeaders } from './auth'

// ---------------------------------------------------------------------------
// Global store
// ---------------------------------------------------------------------------

export interface ModelSettings {
  generateModel: string
  validateModel: string
  monitorModel: string
}

const DEFAULT_MODEL_SETTINGS: ModelSettings = {
  generateModel: 'claude-sonnet-4-6',
  validateModel: 'gpt-4o-mini',
  monitorModel:  'gpt-4o-mini',
}

interface StudioState {
  isRunning: boolean
  step: WorkflowStep
  activity: ActivityEntry[]
  strategyEvents: StrategyRawEvent[]  // raw supervisor events from SSE gateway
  code: string
  savedCode: string          // last persisted version — used to detect unsaved changes
  loadedStrategyName: string | null   // tracks which file code was loaded from
  toolsAvailable: string[]
  activeStrategyId: string | null     // set after submit_strategy succeeds
  modelSettings: ModelSettings
  pendingSubmission: {
    legs: Array<{ leg_id: string; symbol: string; quantity: number; side: string; tif: string }>
    params: Array<{ name: string; type: 'int' | 'float' | 'bool' | 'str'; value: string | number | boolean }>
    isFirstSubmit: boolean
  } | null

  setRunning: (v: boolean) => void
  setStep: (s: WorkflowStep) => void
  addActivity: (e: ActivityEntry) => void
  updateLastActivity: (id: string, patch: Partial<ActivityEntry>) => void
  addStrategyEvent: (e: StrategyRawEvent) => void
  addStrategyEvents: (batch: StrategyRawEvent[]) => void
  clearStrategyEvents: () => void
  setCode: (c: string) => void
  setSavedCode: (c: string) => void
  setLoadedStrategyName: (name: string | null) => void
  setTools: (t: string[]) => void
  setActiveStrategyId: (id: string | null) => void
  setModelSettings: (s: ModelSettings) => void
  setPendingSubmission: (v: StudioState['pendingSubmission']) => void
  reset: () => void
}

const MAX_LIVE_EVENTS = 300

export const useStore = create<StudioState>((set) => ({
  isRunning: false,
  step: 'idle',
  activity: [],
  strategyEvents: [],
  code: '',
  savedCode: '',
  loadedStrategyName: null,
  toolsAvailable: [],
  activeStrategyId: null,
  modelSettings: DEFAULT_MODEL_SETTINGS,
  pendingSubmission: null,

  setRunning: (v) => set({ isRunning: v }),
  setStep: (s) => set({ step: s }),
  addActivity: (e) => set((s) => ({ activity: [...s.activity, e] })),
  updateLastActivity: (id, patch) =>
    set((s) => ({
      activity: s.activity.map((e) => (e.id === id ? { ...e, ...patch } : e)),
    })),
  addStrategyEvent: (e) => set((s) => {
    const next = [...s.strategyEvents, e]
    return { strategyEvents: next.length > MAX_LIVE_EVENTS ? next.slice(-MAX_LIVE_EVENTS) : next }
  }),
  addStrategyEvents: (batch) => set((s) => {
    const next = [...s.strategyEvents, ...batch]
    return { strategyEvents: next.length > MAX_LIVE_EVENTS ? next.slice(-MAX_LIVE_EVENTS) : next }
  }),
  clearStrategyEvents: () => set({ strategyEvents: [] }),
  setCode: (c) => set({ code: c }),
  setSavedCode: (c) => set({ savedCode: c }),
  setLoadedStrategyName: (name) => set({ loadedStrategyName: name }),
  setTools: (t) => set({ toolsAvailable: t }),
  setActiveStrategyId: (id) => set({ activeStrategyId: id }),
  setModelSettings: (s) => set({ modelSettings: s }),
  setPendingSubmission: (v) => set({ pendingSubmission: v }),
  reset: () => set({ isRunning: false, step: 'idle', activity: [], strategyEvents: [], code: '', savedCode: '', loadedStrategyName: null, activeStrategyId: null, pendingSubmission: null }),
}))

// ---------------------------------------------------------------------------
// Streaming fetch helper
// ---------------------------------------------------------------------------

let entryId = 0
const nextId = () => String(++entryId)

async function streamResubmit(
  code: string,
  legs: object[],
  strategyParams: Record<string, unknown>,
  onEvent: (event: StudioEvent) => void,
  signal: AbortSignal,
  monitorModel?: string,
  startTime?: string,
  endTime?: string,
) {
  const response = await fetch('/api/resubmit-strategy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
    body: JSON.stringify({
      code,
      legs,
      strategy_params: strategyParams,
      monitor_model: monitorModel,
      ...(startTime ? { start_time: startTime } : {}),
      ...(endTime   ? { end_time:   endTime   } : {}),
    }),
    signal,
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  if (!response.body) throw new Error('No response body')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      try { onEvent(JSON.parse(line.slice(6)) as StudioEvent) } catch { /* ignore */ }
    }
  }
}


async function streamWorkflow(
  intent: string,
  onEvent: (event: StudioEvent) => void,
  signal: AbortSignal,
  strategyName?: string,
  existingCode?: string,
  workflowMode?: string,
  modelSettings?: ModelSettings,
) {
  const response = await fetch('/api/run-strategy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
    body: JSON.stringify({
      intent,
      ...(strategyName ? { strategy_name: strategyName } : {}),
      ...(existingCode ? { existing_code: existingCode } : {}),
      ...(workflowMode ? { workflow_mode: workflowMode } : {}),
      ...(modelSettings ? {
        generate_model: modelSettings.generateModel,
        validate_model: modelSettings.validateModel,
        monitor_model:  modelSettings.monitorModel,
      } : {}),
    }),
    signal,
  })

  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  if (!response.body) throw new Error('No response body')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      try {
        onEvent(JSON.parse(line.slice(6)) as StudioEvent)
      } catch { /* ignore malformed */ }
    }
  }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

let strategyEventId = 0
const nextStrategyEventId = () => String(++strategyEventId)

export default function App() {
  const {
    isRunning, step, code, modelSettings, activeStrategyId,
    setRunning, setStep, addActivity, updateLastActivity, addStrategyEvent, clearStrategyEvents,
    setCode, setSavedCode, setLoadedStrategyName, setTools, setActiveStrategyId, setModelSettings, setPendingSubmission, reset,
  } = useStore()
  const abortRef = useRef<AbortController | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)
  const [rightPanel, setRightPanel] = useState<'code' | 'qa' | 'logs'>('code')
  const [leftPanel, setLeftPanel] = useState<'activity' | 'events'>('activity')

  const liveTextIdRef = useRef<string | null>(null)
  const liveTextTurnRef = useRef<number | null>(null)

  const openStrategyEventSource = useCallback((strategyId: string) => {
    // Close any existing connection
    if (eventSourceRef.current) {
      eventSourceRef.current.close()
      eventSourceRef.current = null
    }
    clearStrategyEvents()

    const es = new EventSource(`/api/strategies/${strategyId}/events`)
    eventSourceRef.current = es

    // RAF batching: buffer incoming events and flush into the store at most once per frame
    const eventBuffer: import('./types').StrategyRawEvent[] = []
    let rafPending = false
    let terminalPending: { step: 'done'; running: false } | null = null

    const flushBuffer = () => {
      rafPending = false
      if (eventBuffer.length > 0) {
        const batch = eventBuffer.splice(0)
        useStore.getState().addStrategyEvents(batch)
      }
      if (terminalPending) {
        setStep(terminalPending.step)
        setRunning(terminalPending.running)
        terminalPending = null
        es.close()
        eventSourceRef.current = null
      }
    }

    es.onmessage = (ev) => {
      try {
        const raw = JSON.parse(ev.data) as Record<string, unknown>
        const eventType = (raw.event_type ?? raw.type ?? 'unknown') as string
        if (eventType === 'stream_end' || eventType === 'relay_error') {
          es.close()
          eventSourceRef.current = null
          return
        }
        const terminationType = raw.termination_type as 'COMPLETED' | 'EXPIRED' | 'STOPPED' | 'FAILED' | undefined
        eventBuffer.push({
          id: nextStrategyEventId(),
          timestamp: Date.now(),
          eventType,
          strategyId: raw.strategy_id as string | undefined,
          raw,
          terminationType,
        })
        if (terminationType) {
          // Mark terminal — will be acted on in the next flush
          terminalPending = { step: 'done', running: false }
        }
        if (!rafPending) {
          rafPending = true
          requestAnimationFrame(flushBuffer)
        }
      } catch { /* ignore malformed */ }
    }

    // Do NOT call es.close() here — that would cancel the browser's built-in
    // auto-reconnect. Transient drops (network hiccup, server restart) recover
    // automatically. The stream_end / flushBuffer paths still close explicitly
    // when a terminal event or clean shutdown is received.
    es.onerror = () => { /* let browser reconnect */ }
  }, [clearStrategyEvents, setStep, setRunning])

  // Load a strategy into the editor without running
  const handleLoad = useCallback(async (strategyName: string) => {
    const r = await fetch(`/api/strategies/${strategyName}`, { headers: getTraderHeaders() })
    const d = await r.json()
    setCode(d.code)
    setSavedCode(d.code)
    setLoadedStrategyName(strategyName)
  }, [setCode, setSavedCode, setLoadedStrategyName])

  const handleRun = useCallback(async (
    intent: string,
    strategyName?: string,
    existingCode?: string,
    workflowMode?: string,
  ) => {
    reset()
    liveTextIdRef.current = null
    liveTextTurnRef.current = null
    setRunning(true)
    setStep(existingCode ? 'validating' : 'generating')
    if (existingCode) {
      setCode(existingCode)
      setSavedCode(existingCode)
      setLoadedStrategyName(strategyName ?? null)
    }

    abortRef.current = new AbortController()
    let codeBuffer = existingCode ?? ''

    const handleEvent = (event: StudioEvent) => {
      const ts = Date.now()

      switch (event.type) {
        case 'tools_ready':
          setTools(event.tools)
          addActivity({ id: nextId(), type: 'tools_ready', timestamp: ts, content: `${event.count} MCP tools loaded: ${event.tools.join(', ')}` })
          break

        case 'thinking':
          if (liveTextIdRef.current) {
            updateLastActivity(liveTextIdRef.current, { live: false })
            liveTextIdRef.current = null
          }
          addActivity({ id: nextId(), type: 'thinking', timestamp: ts, turn: event.turn, model: event.model })
          break

        case 'text_delta': {
          if (liveTextIdRef.current && liveTextTurnRef.current === event.turn) {
            updateLastActivity(liveTextIdRef.current, {
              content: (useStore.getState().activity.find(e => e.id === liveTextIdRef.current)?.content ?? '') + event.delta,
            })
          } else {
            if (liveTextIdRef.current) {
              updateLastActivity(liveTextIdRef.current, { live: false })
            }
            const id = nextId()
            liveTextIdRef.current = id
            liveTextTurnRef.current = event.turn
            addActivity({ id, type: 'text_delta', timestamp: ts, content: event.delta, turn: event.turn, live: true, model: event.model })
          }
          break
        }

        case 'tool_call_start':
          if (liveTextIdRef.current) {
            updateLastActivity(liveTextIdRef.current, { live: false })
            liveTextIdRef.current = null
          }
          addActivity({ id: nextId(), type: 'tool_call_start', timestamp: ts, toolName: event.name, turn: event.turn })
          break

        case 'tool_call': {
          const derived = toolToStep(event.name)
          if (derived) setStep(derived)
          const inp = event.input as Record<string, unknown>
          const code = (inp.code ?? (inp.config_json as Record<string, unknown>)?.code ?? '') as string
          if (code && code !== codeBuffer) {
            codeBuffer = code
            setCode(codeBuffer)
          }
          // Open SSE event stream as soon as we know the strategy_id — before the HTTP submit fires
          if (event.name === 'submit_strategy' && inp.strategy_id) {
            setStep('monitoring')
            setLeftPanel('events')
            openStrategyEventSource(inp.strategy_id as string)
          }
          addActivity({ id: nextId(), type: 'tool_call', timestamp: ts, toolName: event.name, toolInput: event.input, turn: event.turn })
          break
        }

        case 'tool_executing':
          addActivity({ id: nextId(), type: 'tool_executing', timestamp: ts, toolName: event.name, turn: event.turn })
          break

        case 'tool_result':
          addActivity({ id: nextId(), type: 'tool_result', timestamp: ts, toolName: event.name, toolContent: event.content, durationMs: event.duration_ms, toolFailed: event.failed, turn: event.turn })
          break

        case 'tool_error':
          addActivity({ id: nextId(), type: 'tool_error', timestamp: ts, toolName: event.name, toolError: event.error, durationMs: event.duration_ms, turn: event.turn })
          break

        case 'strategy_submitted':
          setActiveStrategyId(event.strategy_id)
          // EventSource already opened on tool_call — don't reopen
          break

        case 'rate_limit_retry':
          addActivity({ id: nextId(), type: 'rate_limit_retry', timestamp: ts, content: event.message })
          break

        case 'params_ready':
          // Strategy generated + validated — store the code and hand legs/params to the UI
          setCode(event.code)
          setSavedCode(event.code)
          setPendingSubmission({ legs: event.legs, params: event.params, isFirstSubmit: true })
          break

        case 'turn_complete':
          if (liveTextIdRef.current) {
            updateLastActivity(liveTextIdRef.current, { live: false })
            liveTextIdRef.current = null
          }
          break

        case 'done': {
          if (liveTextIdRef.current) {
            updateLastActivity(liveTextIdRef.current, { live: false })
            liveTextIdRef.current = null
          }
          // If a strategy was submitted, stay in monitoring state — EventSource drives done
          const hasActiveStrategy = Boolean(useStore.getState().activeStrategyId)
          if (!hasActiveStrategy) {
            setStep('done')
            setRunning(false)
          }
          break
        }

        case 'stream_end':
          if (!useStore.getState().activeStrategyId) {
            setRunning(false)
          }
          break

        case 'error':
          if (liveTextIdRef.current) {
            updateLastActivity(liveTextIdRef.current, { live: false })
            liveTextIdRef.current = null
          }
          setStep('error')
          setRunning(false)
          addActivity({ id: nextId(), type: 'error', timestamp: ts, content: event.message })
          break
      }
    }

    try {
      await streamWorkflow(intent, handleEvent, abortRef.current.signal, strategyName, existingCode, workflowMode, modelSettings)
    } catch (err: unknown) {
      if (err instanceof Error && err.name !== 'AbortError') {
        setStep('error')
        addActivity({ id: nextId(), type: 'error', timestamp: Date.now(), content: String(err) })
      }
    } finally {
      // Only stop running if no strategy is being monitored
      if (!useStore.getState().activeStrategyId) {
        setRunning(false)
      }
    }
  }, [reset, setRunning, setStep, setCode, setSavedCode, setLoadedStrategyName, setTools, setActiveStrategyId, openStrategyEventSource, setModelSettings, setPendingSubmission, addActivity, updateLastActivity, modelSettings])

  const handleStop = useCallback(() => {
    abortRef.current?.abort()
    // Close SSE event stream
    if (eventSourceRef.current) {
      eventSourceRef.current.close()
      eventSourceRef.current = null
    }
    setRunning(false)
    setStep('done')   // keep 'done' so the post-run panel stays visible
    const { activeStrategyId } = useStore.getState()
    if (activeStrategyId) {
      fetch(`/api/strategies/${activeStrategyId}/stop`, {
        method: 'POST',
        headers: getTraderHeaders(),
      }).catch((err) => console.error('[stop-strategy] failed:', err))
    } else {
      console.warn('[stop] no activeStrategyId — stop_strategy not called')
    }
  }, [setRunning, setStep])

  const handleResubmit = useCallback(async (
    legs: object[],
    strategyParams: Record<string, unknown>,
    currentCode: string,
    startTime?: string,
    endTime?: string,
  ) => {
    if (!currentCode) {
      console.error('[resubmit] no code — aborting')
      return
    }
    // Clear activity feed and refs without wiping code
    liveTextIdRef.current = null
    liveTextTurnRef.current = null
    useStore.setState({ activity: [], activeStrategyId: null })
    setRunning(true)
    setStep('submitting')
    setCode(currentCode)

    abortRef.current = new AbortController()

    const handleEvent = (event: StudioEvent) => {
      const ts = Date.now()
      switch (event.type) {
        case 'thinking':
          if (liveTextIdRef.current) { updateLastActivity(liveTextIdRef.current, { live: false }); liveTextIdRef.current = null }
          addActivity({ id: nextId(), type: 'thinking', timestamp: ts, turn: event.turn, model: event.model })
          break
        case 'text_delta': {
          if (liveTextIdRef.current && liveTextTurnRef.current === event.turn) {
            updateLastActivity(liveTextIdRef.current, { content: (useStore.getState().activity.find(e => e.id === liveTextIdRef.current)?.content ?? '') + event.delta })
          } else {
            if (liveTextIdRef.current) updateLastActivity(liveTextIdRef.current, { live: false })
            const id = nextId()
            liveTextIdRef.current = id
            liveTextTurnRef.current = event.turn
            addActivity({ id, type: 'text_delta', timestamp: ts, content: event.delta, turn: event.turn, live: true, model: event.model })
          }
          break
        }
        case 'tool_call': {
          const derived = toolToStep(event.name)
          if (derived) setStep(derived)
          const inp = event.input as Record<string, unknown>
          const code = (inp.code ?? (inp.config_json as Record<string, unknown>)?.code ?? '') as string
          if (code) setCode(code)
          if (event.name === 'submit_strategy' && inp.strategy_id) {
            setStep('monitoring')
            setLeftPanel('events')
            openStrategyEventSource(inp.strategy_id as string)
          }
          addActivity({ id: nextId(), type: 'tool_call', timestamp: ts, toolName: event.name, toolInput: event.input, turn: event.turn })
          break
        }
        case 'tool_executing': addActivity({ id: nextId(), type: 'tool_executing', timestamp: ts, toolName: event.name, turn: event.turn }); break
        case 'tool_result':   addActivity({ id: nextId(), type: 'tool_result',    timestamp: ts, toolName: event.name, toolContent: event.content, durationMs: event.duration_ms, toolFailed: event.failed, turn: event.turn }); break
        case 'tool_error':    addActivity({ id: nextId(), type: 'tool_error',     timestamp: ts, toolName: event.name, toolError: event.error,   durationMs: event.duration_ms, turn: event.turn }); break
        case 'strategy_submitted':
          setActiveStrategyId(event.strategy_id)
          // EventSource already opened on tool_call — don't reopen
          break
        case 'turn_complete':
          if (liveTextIdRef.current) { updateLastActivity(liveTextIdRef.current, { live: false }); liveTextIdRef.current = null }
          break
        case 'done':
          if (liveTextIdRef.current) { updateLastActivity(liveTextIdRef.current, { live: false }); liveTextIdRef.current = null }
          // Stay running if monitoring via EventSource
          if (!useStore.getState().activeStrategyId) { setStep('done'); setRunning(false) }
          break
        case 'stream_end':
          if (!useStore.getState().activeStrategyId) setRunning(false)
          break
        case 'error':
          if (liveTextIdRef.current) { updateLastActivity(liveTextIdRef.current, { live: false }); liveTextIdRef.current = null }
          setStep('error'); setRunning(false)
          addActivity({ id: nextId(), type: 'error', timestamp: ts, content: event.message })
          break
      }
    }

    try {
      await streamResubmit(currentCode, legs, strategyParams, handleEvent, abortRef.current.signal, undefined, startTime, endTime)
    } catch (err: unknown) {
      if (err instanceof Error && err.name !== 'AbortError') {
        setStep('error')
        addActivity({ id: nextId(), type: 'error', timestamp: Date.now(), content: String(err) })
      }
    } finally {
      if (!useStore.getState().activeStrategyId) setRunning(false)
    }
  }, [reset, setRunning, setStep, setCode, setActiveStrategyId, openStrategyEventSource, addActivity, updateLastActivity])

  const trader = getTrader()
  const [showClearRuns, setShowClearRuns] = useState(false)

  return (
    <LoginGate>
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden' }}>
      <header style={{
        display: 'flex', alignItems: 'center', gap: 12,
        padding: '0 16px', height: 44,
        background: 'var(--surface)', borderBottom: '1px solid var(--border)',
        flexShrink: 0,
      }}>
        <img src="/lumitecLargeLogo.jpeg" alt="Lumitec" style={{ height: 28, objectFit: 'contain' }} />
        <span style={{ color: 'var(--text-dim)', fontWeight: 400 }}>Strategy Studio</span>
        <div style={{ flex: 1 }} />
        <button
          onClick={() => setShowClearRuns(true)}
          title="Manage run databases"
          style={{ padding: '3px 10px', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text-dim)', fontSize: 11, cursor: 'pointer' }}
        >
          🗄 Runs
        </button>
        <StatusStepper step={step} />
        {!isRunning && step !== 'idle' && (
          <button
            onClick={() => reset()}
            style={{ marginLeft: 8, padding: '3px 10px', background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--text-dim)', fontSize: 11, cursor: 'pointer' }}
          >
            ↺ New Run
          </button>
        )}
        <div style={{
          width: 8, height: 8, borderRadius: '50%',
          background: isRunning ? 'var(--green)' : step === 'error' ? 'var(--red)' : 'var(--text-muted)',
          boxShadow: isRunning ? '0 0 6px var(--green)' : 'none',
        }} />
        {trader && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 8, borderLeft: '1px solid var(--border)', paddingLeft: 12 }}>
            <span style={{ fontSize: 11, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
              {trader.traderId} <span style={{ color: 'var(--text-muted)' }}>@ {trader.orgId}</span>
            </span>
            <button
              onClick={() => { clearTrader(); window.location.reload() }}
              style={{ padding: '2px 8px', background: 'transparent', border: '1px solid var(--border)', borderRadius: 3, color: 'var(--text-muted)', fontSize: 10, cursor: 'pointer' }}
            >
              ⏏
            </button>
          </div>
        )}
      </header>

      <IntentInput onRun={handleRun} onLoad={handleLoad} onStop={handleStop} onResubmit={handleResubmit} isRunning={isRunning} editorCode={code} modelSettings={modelSettings} onModelSettingsChange={setModelSettings} />

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <div style={{ width: '45%', borderRight: '1px solid var(--border)', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
            {(['activity', 'events'] as const).map(panel => (
              <button key={panel} onClick={() => setLeftPanel(panel)} style={{
                padding: '8px 14px', fontSize: 11, fontFamily: 'var(--font-mono)', letterSpacing: '0.1em',
                fontWeight: 600, border: 'none', borderBottom: leftPanel === panel ? '2px solid var(--accent)' : '2px solid transparent',
                background: 'transparent', color: leftPanel === panel ? 'var(--accent)' : 'var(--text-dim)',
                cursor: 'pointer',
              }}>
                {panel === 'activity' ? 'AGENT ACTIVITY' : 'STRATEGY EVENTS'}
              </button>
            ))}
          </div>
          {leftPanel === 'activity' ? <ActivityFeed /> : <StrategyEventsFeed />}
        </div>

        <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <div style={{ display: 'flex', alignItems: 'center', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
            {(['code', 'qa', 'logs'] as const).map(panel => (
              <button key={panel} onClick={() => setRightPanel(panel)} style={{
                padding: '8px 14px', fontSize: 11, fontFamily: 'var(--font-mono)', letterSpacing: '0.1em',
                fontWeight: 600, border: 'none', borderBottom: rightPanel === panel ? '2px solid var(--accent)' : '2px solid transparent',
                background: 'transparent', color: rightPanel === panel ? 'var(--accent)' : 'var(--text-dim)',
                cursor: 'pointer',
              }}>
                {panel === 'code' ? 'STRATEGY CODE' : panel === 'qa' ? 'RUN Q&A' : 'LOGS'}
              </button>
            ))}
          </div>
          {rightPanel === 'code' ? <CodePanel /> : rightPanel === 'qa' ? (
            <RunQA strategyId={activeStrategyId} modelSettings={modelSettings} />
          ) : (
            <StrategyLogs strategyId={activeStrategyId} />
          )}
        </div>
      </div>
      {showClearRuns && <ClearRunsDialog onClose={() => setShowClearRuns(false)} />}
    </div>
    </LoginGate>
  )
}
