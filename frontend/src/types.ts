// SSE event types emitted by the backend

export type StudioEventType =
  | 'tools_ready'
  | 'thinking'
  | 'text_delta'
  | 'tool_call_start'
  | 'tool_call'
  | 'tool_executing'
  | 'tool_result'
  | 'tool_error'
  | 'turn_complete'
  | 'rate_limit_retry'
  | 'strategy_submitted'
  | 'sim_commentary'
  | 'params_ready'
  | 'done'
  | 'stream_end'
  | 'error'

export interface ToolsReadyEvent { type: 'tools_ready'; tools: string[]; count: number }
export interface ThinkingEvent { type: 'thinking'; turn: number; model?: string }
export interface TextDeltaEvent { type: 'text_delta'; delta: string; turn: number; model?: string }
export interface ToolCallStartEvent { type: 'tool_call_start'; id: string; name: string; turn: number }
export interface ToolCallEvent { type: 'tool_call'; id: string; name: string; input: Record<string, unknown>; turn: number }
export interface ToolExecutingEvent { type: 'tool_executing'; name: string; turn: number }
export interface ToolResultEvent { type: 'tool_result'; id: string; name: string; content: string; turn: number; duration_ms?: number; failed?: boolean }
export interface ToolErrorEvent { type: 'tool_error'; id: string; name: string; error: string; turn: number; duration_ms?: number }
export interface TurnCompleteEvent { type: 'turn_complete'; turn: number; stop_reason: string }
export interface RateLimitRetryEvent { type: 'rate_limit_retry'; attempt: number; retry_in: number; message: string }
export interface StrategySubmittedEvent { type: 'strategy_submitted'; strategy_id: string }
export interface SimCommentaryEvent {
  type: 'sim_commentary'
  commentary: string
  position?: number
  realized_pnl?: number
  violations?: Array<{ rule: string; detail: string; severity: string }>
  terminated?: boolean
  termination_reason?: string | null
  turn: number
}
export interface ParamsReadyEvent {
  type: 'params_ready'
  code: string
  legs: Array<{ leg_id: string; symbol: string; quantity: number; side: string; tif: string }>
  params: Array<{ name: string; type: 'int' | 'float' | 'bool' | 'str'; value: string | number | boolean }>
  turn: number
}
export interface DoneEvent { type: 'done' }
export interface StreamEndEvent { type: 'stream_end' }
export interface ErrorEvent { type: 'error'; message: string }

export type StudioEvent =
  | ToolsReadyEvent | ThinkingEvent | TextDeltaEvent
  | ToolCallStartEvent | ToolCallEvent | ToolExecutingEvent
  | ToolResultEvent | ToolErrorEvent | TurnCompleteEvent
  | RateLimitRetryEvent | StrategySubmittedEvent | SimCommentaryEvent
  | ParamsReadyEvent | DoneEvent | StreamEndEvent | ErrorEvent

// Raw supervisor event from SSE gateway (port 9001 → relayed via 8089)
export interface StrategyRawEvent {
  id: string                          // generated client-side
  timestamp: number                   // client-side receive time
  eventType: string                   // event_type from supervisor
  strategyId?: string
  raw: Record<string, unknown>        // full raw event object from gateway
  terminationType?: string            // enriched by backend for terminal events
}

// Activity feed entry
export interface ActivityEntry {
  id: string
  type: StudioEventType
  timestamp: number
  content?: string        // text (accumulated), tools_ready, thinking, error
  model?: string
  toolName?: string
  toolInput?: Record<string, unknown>
  toolContent?: string
  toolFailed?: boolean
  toolError?: string
  durationMs?: number
  turn?: number
  live?: boolean          // true while text is still streaming in
  simCommentary?: string
  simPosition?: number
  simPnl?: number
  simViolations?: Array<{ rule: string; detail: string; severity: string }>
}

export type WorkflowStep =
  | 'idle' | 'generating' | 'validating' | 'submitting'
  | 'monitoring' | 'publishing' | 'done' | 'error'

export function toolToStep(toolName: string): WorkflowStep | null {
  if (toolName.includes('validate')) return 'validating'
  if (toolName.includes('submit')) return 'submitting'
  if (toolName.includes('stream') || toolName.includes('events')) return 'monitoring'
  if (toolName.includes('publish')) return 'publishing'
  return null
}
