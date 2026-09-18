import { useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import type * as MonacoType from 'monaco-editor'
import { useStore } from '../App'
import { authHeaders } from '../auth/cognito'
import { ConfirmDialog, AlertDialog, type PublishFieldError } from './Dialogs'

export default function CodePanel() {
  const code = useStore((s) => s.code)
  const savedCode = useStore((s) => s.savedCode)
  const isRunning = useStore((s) => s.isRunning)
  const loadedStrategyName = useStore((s) => s.loadedStrategyName)
  const setCode = useStore((s) => s.setCode)
  const setSavedCode = useStore((s) => s.setSavedCode)
  const setLoadedStrategyName = useStore((s) => s.setLoadedStrategyName)

  const editorRef = useRef<MonacoType.editor.IStandaloneCodeEditor | null>(null)

  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saved' | 'error'>('idle')
  const [publishStatus, setPublishStatus] = useState<'idle' | 'ok'>('idle')
  const [publishInfo, setPublishInfo] = useState<
    { name: string; logicId?: string; version?: string; sha256?: string; revision?: number; visibility?: string; unchanged?: boolean } | null
  >(null)
  // Blocking failure dialog — publish is deliberate + infrequent, so a failure
  // must be acknowledged rather than flashing past as a toast.
  const [publishAlert, setPublishAlert] = useState<
    { title: string; message: string; errors: PublishFieldError[] } | null
  >(null)
  // "platform" is admin-assigned only — deliberately not offered in the Studio UI.
  const [visibility, setVisibility] = useState<'private' | 'shared' | 'public'>('private')
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
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
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
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
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
    setPublishInfo(null)
    try {
      const r = await fetch('/api/publish-strategy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ name: publishName, code, visibility }),
      })
      const data = await r.json().catch(() => null)

      if (!r.ok) {
        // backend wraps the strategy server's reply as { detail: { error } };
        // `error` is either a string or { message, errors: [{phase,message,line}] }
        const err = data?.detail?.error ?? data?.detail ?? data
        let message = `Publish failed (HTTP ${r.status}).`
        let errors: PublishFieldError[] = []
        if (typeof err === 'string') {
          message = err
        } else if (err && typeof err === 'object') {
          if (typeof err.message === 'string') message = err.message
          if (Array.isArray(err.errors)) errors = err.errors as PublishFieldError[]
        }
        setPublishAlert({ title: 'Publish failed', message, errors })
        return
      }

      setPublishInfo({
        name: typeof data?.name === 'string' ? data.name : publishName,
        logicId: data?.logic_id,
        version: data?.version ?? data?.upstream?.user_version,
        sha256: data?.sha256,
        revision: data?.upstream?.revision,
        visibility: data?.visibility ?? visibility,
        // Registry no-ops a republish whose content exactly matches the latest
        // revision (no new version minted) — surface that instead of implying
        // a new version was just created.
        unchanged: data?.upstream?.status === 'unchanged',
      })
      setPublishStatus('ok')
      setTimeout(() => setPublishStatus('idle'), 8000)
    } catch {
      setPublishAlert({
        title: 'Publish failed',
        message: 'Could not reach the Studio backend. Check that it is running and try again.',
        errors: [],
      })
    } finally {
      setPublishing(false)
    }
  }, [code, publishing, inferDefaultName, loadedStrategyName, visibility])

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
      {publishAlert && (
        <AlertDialog
          title={publishAlert.title}
          message={publishAlert.message}
          errors={publishAlert.errors}
          onOk={() => setPublishAlert(null)}
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
        {publishStatus === 'ok' && publishInfo && (
          <span
            onClick={() => setPublishStatus('idle')}
            title={[
              publishInfo.logicId ? `logic_id: ${publishInfo.logicId}` : '',
              publishInfo.sha256 ? `sha256: ${publishInfo.sha256}` : '',
              publishInfo.unchanged ? 'code is identical to the already-published version — no new version was created' : '',
              'click to dismiss',
            ].filter(Boolean).join('\n')}
            style={{ fontSize: 10, color: publishInfo.unchanged ? 'var(--text-dim)' : 'var(--green)', fontFamily: 'var(--font-mono)', cursor: 'pointer' }}
          >
            {publishInfo.unchanged ? `↔ no changes — ${publishInfo.name}` : `✓ published ${publishInfo.name}`}
            {publishInfo.version
              ? ` · v${publishInfo.version}`
              : publishInfo.revision != null ? ` · rev ${publishInfo.revision}` : ''}
            {publishInfo.sha256 ? ` · #${publishInfo.sha256.slice(0, 8)}` : ''}
            {publishInfo.visibility ? ` · ${publishInfo.visibility}` : ''}
          </span>
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

            <select
              value={visibility}
              onChange={(e) => setVisibility(e.target.value as 'private' | 'shared' | 'public')}
              disabled={publishing || saving}
              title="Who can see this strategy once published"
              style={{
                padding: '2px 4px',
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
              <option value="private">private</option>
              <option value="shared">shared</option>
              <option value="public">public</option>
            </select>

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
        <div
          onClick={() => editorRef.current?.focus()}
          style={{
            position: 'absolute', top: '50%', left: '50%',
            transform: 'translate(-50%, -50%)',
            color: 'var(--text-muted)', fontSize: 12,
            textAlign: 'center', lineHeight: 1.8,
            cursor: 'text',
          }}
        >
          Click here or use CODE mode to paste your strategy
        </div>
      )}

      <Editor
        height="100%"
        language="python"
        value={code}
        theme="vs-dark"
        onMount={(editor) => { editorRef.current = editor }}
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
