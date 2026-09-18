// Small shared modal primitives — extracted from CodePanel.tsx so
// IntentInput.tsx (unpublish/purge confirmation + error alerts) can reuse
// the same look instead of duplicating it.

export function ConfirmDialog({ message, onYes, onNo }: { message: string; onYes: () => void; onNo: () => void }) {
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

export type PublishFieldError = { phase?: string; message: string; line?: number }

export function AlertDialog({
  title, message, errors, onOk,
}: { title: string; message: string; errors?: PublishFieldError[]; onOk: () => void }) {
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
        minWidth: 320,
        maxWidth: 500,
        display: 'flex', flexDirection: 'column', gap: 14,
      }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)' }}>{title}</div>
        <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{message}</div>
        {errors && errors.length > 0 && (
          <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
            {errors.map((e, i) => (
              <li key={i} style={{
                fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--red)',
                background: 'var(--surface-2)', border: '1px solid var(--border)',
                borderRadius: 4, padding: '6px 8px', lineHeight: 1.4,
              }}>
                {e.phase ? `[${e.phase}] ` : ''}{e.message}{e.line != null ? `  (line ${e.line})` : ''}
              </li>
            ))}
          </ul>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button onClick={onOk} style={{
            padding: '5px 18px', borderRadius: 4, fontSize: 12, fontWeight: 600,
            background: 'var(--accent)', border: 'none', color: '#fff', cursor: 'pointer',
          }}>OK</button>
        </div>
      </div>
    </div>
  )
}
