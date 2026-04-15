import type { WorkflowStep } from '../types'

const STEPS: { key: WorkflowStep; label: string }[] = [
  { key: 'generating', label: 'Generate' },
  { key: 'validating', label: 'Validate' },
  { key: 'submitting', label: 'Simulate' },
  { key: 'monitoring', label: 'Monitor' },
  { key: 'publishing', label: 'Publish' },
]

const ORDER: WorkflowStep[] = ['generating', 'validating', 'submitting', 'monitoring', 'publishing', 'done']

function stepIndex(step: WorkflowStep) {
  return ORDER.indexOf(step)
}

interface Props {
  step: WorkflowStep
}

export default function StatusStepper({ step }: Props) {
  const current = stepIndex(step)

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
      {STEPS.map(({ key, label }, i) => {
        const idx = ORDER.indexOf(key)
        const isDone = current > idx || step === 'done'
        const isActive = step === key
        const isPending = current < idx && step !== 'error'

        return (
          <div key={key} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && (
              <div style={{ width: 16, height: 1, background: isDone ? 'var(--accent)' : 'var(--border)', margin: '0 2px' }} />
            )}
            <div style={{
              padding: '2px 7px',
              borderRadius: 3,
              fontSize: 10,
              fontFamily: 'var(--font-mono)',
              fontWeight: 600,
              letterSpacing: '0.05em',
              background: isActive ? 'var(--accent-dim)' : isDone ? '#1a2e1a' : 'transparent',
              color: isActive ? 'var(--accent)' : isDone ? 'var(--green)' : isPending ? 'var(--text-muted)' : 'var(--text-dim)',
              border: `1px solid ${isActive ? 'var(--accent)' : isDone ? 'var(--green)' : 'transparent'}`,
              transition: 'all 0.2s',
            }}>
              {isDone ? '✓ ' : isActive ? '⟳ ' : ''}{label}
            </div>
          </div>
        )
      })}
    </div>
  )
}
