import { useState, useCallback } from 'react'
import Editor from '@monaco-editor/react'
import { useStore } from '../App'
import { getTraderHeaders } from '../auth'

function ConfirmDialog({ message, onYes, onNo }: { message: string; onYes: () => void; onNo: () => void }) {
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000,
      background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '20px 24px',
        minWidth: 300,
        maxWidth: 420,
        display: 'flex', flexDirection: 'column', gap: 16,
      }}>
        <div style={{ fontSize: 13, color: 'var(--text)', lineHeight: 1.5 }}>{message}</div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button onClick={onNo} style={{
            padding: '5px 18px', borderRadius: 4, fontSize: 12, fontWeight: 600,
            background: 'var(--surface-2)', border: '1px solid var(--border)',
            color: 'var(--text-dim)', cursor: 'pointer',
          }}>No</button>
          <button onClick={onYes} style={{
            padding: '5px 18px', borderRadius: 4, fontSize: 12, fontWeight: 600,
            background: 'var(--accent)', border: 'none',
            color: '#fff', cursor: 'pointer',
          }}>Yes</button>
        </div>
      </div>
    </div>
  )
}

export default function CodePanel() {
  const code = useStore((s) => s.code)
  const savedCode = useStore((s) => s.savedCode)
  const isRunning = useStore((s) => s.isRunning)
  const loadedStrategyName = useStore((s) => s.loadedStrategyName)
  const setCode = useStore((s) => s.setCode)
  const setSavedCode = useStore((s) => s.setSavedCode)
  const setLoadedStrategyName = useStore((s) => s.setLoadedStrategyName)

  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saved' | 'error'>('idle')
  const [publishStatus, setPublishStatus] = useState<'idle' | 'ok' | 'error'>('idle')
  const [publishedName, setPublishedName] = useState<string | null>(null)
  const [showCloseConfirm, setShowCloseConfirm] = useState(false)

  const isDirty = loadedStrategyName !== null && code !== savedCode

  const inferDefaultName = useCallback((): string => {
    if (loadedStrategyName) return loadedStrategyName
    if (!code) return 'my_strategy'

    const fileNameMatch = code.match(/file_name\s*:\s*str\s*=\s*["']([^"']+)["']/)
    if (fileNameMatch?.[1]) {
      return fileNameMatch[1].replace(/\.py$/i, '')
    }

    const classMatch = code.match(/class\s+(\w+)\s*\(LumitecBaseStrategy\)/)
    if (classMatch?.[1]) {
      return classMatch[1]
        .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
        .toLowerCase()
    }

    return 'my_strategy'
  }, [loadedStrategyName, code])

  const save = useCallback(async (): Promise<boolean> => {
    if (!loadedStrategyName || !code || saving) return false
    setSaving(true)
    setSaveStatus('idle')
    try {
      const r = await fetch(`/api/strategies/${loadedStrategyName}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
        body: JSON.stringify({ code }),
      })
      if (r.ok) {
        setSavedCode(code)
        setSaveStatus('saved')
        setTimeout(() => setSaveStatus('idle'), 2000)
        return true
      } else {
        setSaveStatus('error')
        setTimeout(() => setSaveStatus('idle'), 2000)
        return false
      }
    } catch {
      setSaveStatus('error')
      setTimeout(() => setSaveStatus('idle'), 2000)
      return false
    } finally {
      setSaving(false)
    }
  }, [loadedStrategyName, code, saving, setSavedCode])

  const saveAs = useCallback(async (): Promise<boolean> => {
    if (!code || saving) return false

    const defaultName = inferDefaultName()
    const raw = window.prompt('Save strategy as (without .py)', defaultName)
    if (raw === null) return false

    const name = raw.trim().replace(/\.py$/i, '')
    if (!name) return false

    setSaving(true)
    setSaveStatus('idle')
    try {
      const r = await fetch(`/api/strategies/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
        body: JSON.stringify({ code }),
      })
      if (r.ok) {
        setLoadedStrategyName(name)
        setSavedCode(code)
        setSaveStatus('saved')
        setTimeout(() => setSaveStatus('idle'), 2000)
        return true
      }

      setSaveStatus('error')
      setTimeout(() => setSaveStatus('idle'), 2000)
      return false
    } catch {
      setSaveStatus('error')
      setTimeout(() => setSaveStatus('idle'), 2000)
      return false
    } finally {
      setSaving(false)
    }
  }, [code, saving, inferDefaultName, setLoadedStrategyName, setSavedCode])

  const publish = useCallback(async (): Promise<void> => {
    if (!code || publishing) return

    const defaultName = inferDefaultName()
    const name = loadedStrategyName ?? defaultName
    const validName = /^[A-Za-z0-9_]{1,128}$/.test(name) ? name : ''
    const publishName = validName || window.prompt('Publish strategy name (without .py)', defaultName)?.trim().replace(/\.py$/i, '')
    if (!publishName) return

    setPublishing(true)
    setPublishStatus('idle')
    setPublishedName(null)
    try {
      const r = await fetch('/api/publish-strategy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getTraderHeaders() },
        body: JSON.stringify({ name: publishName, code }),
      })
      if (!r.ok) {
        setPublishStatus('error')
        setTimeout(() => setPublishStatus('idle'), 4000)
        return
      }

      const data = await r.json()
      const name = typeof data?.name === 'string' ? data.name : publishName
      setPublishedName(name)
      setPublishStatus('ok')
      setTimeout(() => setPublishStatus('idle'), 6000)
    } catch {
      setPublishStatus('error')
      setTimeout(() => setPublishStatus('idle'), 4000)
    } finally {
      setPublishing(false)
    }
  }, [code, publishing, inferDefaultName, loadedStrategyName])

  const doClose = useCallback(() => {
    setCode('')
    setSavedCode('')
    setLoadedStrategyName(null)
  }, [setCode, setSavedCode, setLoadedStrategyName])

  const handleClose = useCallback(() => {
    if (isDirty) {
      setShowCloseConfirm(true)
    } else {
      doClose()
    }
  }, [isDirty, doClose])

  const handleConfirmYes = useCallback(async () => {
    setShowCloseConfirm(false)
    const ok = await save()
    if (ok) doClose()
  }, [save, doClose])

  const handleConfirmNo = useCallback(() => {
    setShowCloseConfirm(false)
    doClose()
  }, [doClose])

  return (
    <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
      {showCloseConfirm && (
        <ConfirmDialog
          message={`"${loadedStrategyName}.py" has unsaved changes. Save before closing?`}
          onYes={handleConfirmYes}
          onNo={handleConfirmNo}
        />
      )}
      {/* Top-right badge row */}
      <div style={{
        position: 'absolute', top: 8, right: 12, zIndex: 10,
        display: 'flex', gap: 6, alignItems: 'center',
      }}>
        {/* Save status */}
        {saveStatus === 'saved' && (
          <span style={{ fontSize: 10, color: 'var(--green)', fontFamily: 'var(--font-mono)' }}>✓ saved</span>
        )}
        {saveStatus === 'error' && (
          <span style={{ fontSize: 10, color: 'var(--red)', fontFamily: 'var(--font-mono)' }}>✗ save failed</span>
        )}
        {publishStatus === 'ok' && (
          <span style={{ fontSize: 10, color: 'var(--green)', fontFamily: 'var(--font-mono)' }}>
            ✓ published{publishedName ? `: ${publishedName}` : ''}
          </span>
        )}
        {publishStatus === 'error' && (
          <span style={{ fontSize: 10, color: 'var(--red)', fontFamily: 'var(--font-mono)' }}>✗ publish failed</span>
        )}

        {/* Unsaved indicator */}
        {isDirty && saveStatus === 'idle' && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>● unsaved</span>
        )}

        {/* Save button — only when a named strategy is loaded and not running */}
        {!isRunning && code && (
          <>
            <button
              onClick={saveAs}
              disabled={saving}
              title="Save strategy under a filename"
              style={{
                padding: '2px 10px',
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 3,
                color: saving ? 'var(--text-muted)' : 'var(--text-dim)',
                fontSize: 11,
                fontFamily: 'var(--font-mono)',
                fontWeight: 600,
                cursor: saving ? 'default' : 'pointer',
              }}
            >
              Save As
            </button>

            {loadedStrategyName && (
              <button
                onClick={save}
                disabled={saving || publishing || !isDirty}
                title={`Save back to ${loadedStrategyName}.py`}
                style={{
                  padding: '2px 10px',
                  background: 'var(--surface-2)',
                  border: '1px solid var(--border)',
                  borderRadius: 3,
                  color: saving || publishing || !isDirty ? 'var(--text-muted)' : 'var(--text-dim)',
                  fontSize: 11,
                  fontFamily: 'var(--font-mono)',
                  fontWeight: 600,
                  cursor: saving || publishing || !isDirty ? 'default' : 'pointer',
                }}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            )}

            <button
              onClick={publish}
              disabled={publishing || saving}
              title="Publish current editor code to strategy server"
              style={{
                padding: '2px 10px',
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 3,
                color: publishing || saving ? 'var(--text-muted)' : 'var(--text-dim)',
                fontSize: 11,
                fontFamily: 'var(--font-mono)',
                fontWeight: 600,
                cursor: publishing || saving ? 'default' : 'pointer',
              }}
            >
              {publishing ? 'Publishing…' : 'Publish'}
            </button>

            <button
              onClick={handleClose}
              disabled={saving || publishing}
              title="Close strategy"
              style={{
                padding: '2px 10px',
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 3,
                color: saving || publishing ? 'var(--text-muted)' : 'var(--text-dim)',
                fontSize: 11,
                fontFamily: 'var(--font-mono)',
                fontWeight: 600,
                cursor: saving || publishing ? 'default' : 'pointer',
              }}
            >
              Close
            </button>
          </>
        )}

        {/* LIVE badge when running */}
        {isRunning && (
          <div style={{
            padding: '2px 8px',
            background: 'var(--accent-dim)',
            color: 'var(--accent)',
            borderRadius: 3,
            fontSize: 10,
            fontFamily: 'var(--font-mono)',
            fontWeight: 600,
            letterSpacing: '0.05em',
          }}>
            LIVE
          </div>
        )}
      </div>

      {/* Empty state hint */}
      {!code && !isRunning && (
        <div style={{
          position: 'absolute', top: '50%', left: '50%',
          transform: 'translate(-50%, -50%)',
          color: 'var(--text-muted)', fontSize: 12,
          pointerEvents: 'none', textAlign: 'center', lineHeight: 1.8,
        }}>
          Paste or type your strategy code here
          {isRunning && <div style={{ color: 'var(--accent)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>Claude is writing…</div>}
        </div>
      )}

      <Editor
        height="100%"
        language="python"
        value={code}
        theme="vs-dark"
        onChange={(val) => { if (!isRunning && val !== undefined) setCode(val) }}
        options={{
          readOnly: isRunning,
          minimap: { enabled: false },
          fontSize: 12,
          lineHeight: 18,
          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
          scrollBeyondLastLine: false,
          padding: { top: 12 },
          renderLineHighlight: 'none',
          overviewRulerBorder: false,
          hideCursorInOverviewRuler: true,
          folding: true,
          lineNumbers: 'on',
          renderWhitespace: 'none',
          smoothScrolling: true,
          cursorBlinking: isRunning ? 'expand' : 'blink',
        }}
      />
    </div>
  )
}
