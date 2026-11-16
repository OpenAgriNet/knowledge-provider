import { useState } from 'react'
import { Button } from '../components/ui/button'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'

const labels = {
  awaiting_worker: 'Waiting for worker',
  polling: 'Poll in progress',
  retrying: 'Waiting to retry',
  scheduled: 'Scheduled',
}

export default function ScheduledSourceStatus({ provider }) {
  const [snapshot, setSnapshot] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const sources = snapshot?.sources

  async function loadStatus() {
    setBusy(true)
    setError('')
    try {
      const result = await fetchJson(`/providers/${encodeURIComponent(provider)}/scheduled-sources`)
      setSnapshot(result)
    } catch (failure) {
      setSnapshot(null)
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="space-y-2 border-t border-border pt-4" aria-label="Scheduled intake">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Scheduled intake</h3>
        <Button type="button" variant="outline" size="sm" onClick={loadStatus} disabled={busy}>
          {busy ? 'Checking…' : snapshot === null ? 'Check status' : 'Refresh status'}
        </Button>
      </div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {snapshot && <p className="text-xs text-muted-foreground">Checked {formatCompactDateTime(snapshot.checked_at)}</p>}
      {sources?.length === 0 && <p className="text-sm text-muted-foreground">No scheduled sources configured.</p>}
      {sources?.length > 0 && (
        <ul className="divide-y divide-border">
          {sources.map(source => (
            <li key={source.source_key} className="space-y-1 py-2 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{source.source_key}</span>
                <span>{labels[source.status]}</span>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>{source.auto_release
                  ? source.release_network_id ? `Automatic release to ${source.release_network_id}` : 'Automatic release'
                  : 'Draft only'}</span>
                {source.last_page_received_at && <span>Last page {formatCompactDateTime(source.last_page_received_at)}</span>}
                {source.last_page_policy_conflicts > 0 && <span>
                  Last page kept the earlier release policy for {source.last_page_policy_conflicts}{' '}
                  {source.last_page_policy_conflicts === 1 ? 'item' : 'items'}
                </span>}
                {source.next_check_at && <span>Next check {formatCompactDateTime(source.next_check_at)}</span>}
                {source.consecutive_failures > 0 && <span>Consecutive failures {source.consecutive_failures}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
