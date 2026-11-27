import { useCallback, useEffect, useState } from 'react'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'

export default function DocumentRevisionPanel({ workflowId, initialRevisionId, canRelease }) {
  const base = `/documents/${encodeURIComponent(workflowId)}`
  const [history, setHistory] = useState(null)
  const [revision, setRevision] = useState(null)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      setHistory(await fetchJson(`${base}/revisions`))
    } catch (failure) {
      setError(failure.message)
    }
  }, [base])

  useEffect(() => { load() }, [load])

  const inspect = useCallback(async (revisionId) => {
    setPending(true)
    setError('')
    try {
      setRevision(await fetchJson(`${base}/revisions/${encodeURIComponent(revisionId)}`))
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }, [base])

  useEffect(() => { if (initialRevisionId) inspect(initialRevisionId) }, [initialRevisionId, inspect])

  async function changeRelease(networkId, release, withdraw = false) {
    setPending(true)
    setError('')
    setMessage('')
    try {
      const path = `${base}/releases/${encodeURIComponent(networkId)}`
      if (withdraw) {
        await fetchJson(`${path}?revision_id=${encodeURIComponent(release.revision_id)}`, { method: 'DELETE' })
        setMessage(`Withdrawn from ${networkId}.`)
      } else {
        const run = await fetchJson(path, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision_id: revision.revision_id, expected_generation: release?.generation || null }),
        })
        setMessage(`Publication queued on ${networkId}. Workflow ${run.workflow_id}`)
      }
      await load()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }

  const managedNetworks = history?.managed_network_ids
    ?? (history?.managed_network_id ? [history.managed_network_id] : [])

  return (
    <div className="space-y-5 p-4">
      <section className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-medium">Network releases</h2>
          <Button size="sm" variant="outline" disabled={pending} onClick={load}>Refresh</Button>
        </div>
        {history?.releases.map((release) => (
          <div key={release.network_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2 text-xs">
            <span>{release.network_id} · {release.revision_id.slice(0, 8)}</span>
            <Badge variant="outline">{release.withdrawn_at ? 'Withdrawn' : 'Published'}</Badge>
            {canRelease && managedNetworks.includes(release.network_id) && !release.withdrawn_at && (
              <Button size="sm" variant="outline" disabled={pending} onClick={() => changeRelease(release.network_id, release, true)}>
                Withdraw from {release.network_id}
              </Button>
            )}
          </div>
        ))}
        {history && !history.releases.length && <p className="text-xs text-muted-foreground">No network release yet.</p>}
        {canRelease && revision && managedNetworks.map((network) => {
          const release = history.releases.find(({ network_id }) => network_id === network)
          if (release && !release.withdrawn_at && release.revision_id === revision.revision_id) return null
          return (
            <Button key={network} size="sm" disabled={pending} onClick={() => changeRelease(network, release)}>
              Publish selected revision to {network}
            </Button>
          )
        })}
        {message && <p className="break-words text-xs" role="status">{message}</p>}
        {error && <p className="text-xs text-destructive" role="alert">{error}</p>}
      </section>
      <section className="space-y-2">
        <h2 className="text-sm font-medium">Revision history</h2>
        {history && !history.revisions.length && <p className="text-xs text-muted-foreground">Revisions appear after indexing begins.</p>}
        {history?.revisions.map((entry) => (
          <Button key={entry.id} variant={entry.id === revision?.revision_id ? 'secondary' : 'outline'} size="sm"
            disabled={pending} className="mr-2" onClick={() => inspect(entry.id)}>
            {entry.id.slice(0, 8)} · {formatCompactDateTime(entry.created_at)}
          </Button>
        ))}
        {revision && (
          <div className="space-y-3 rounded-md border p-3 text-sm">
            <p className="font-medium">{revision.metadata.name || revision.filename}</p>
            <p className="text-xs text-muted-foreground">{revision.pages.length} pages · {revision.passage_count} passages</p>
            {revision.source_url && <a className="break-all text-xs underline" href={revision.source_url} target="_blank" rel="noreferrer">Open source</a>}
            {revision.pages.map((page) => (
              <details key={page.page_number} className="border-t pt-2">
                <summary className="cursor-pointer text-xs">Page {page.page_number} · {page.detected_language || 'Language undetermined'}</summary>
                <p className="mt-2 whitespace-pre-wrap">{page.edited_markdown || page.original_markdown}</p>
                {Object.entries(page.translations || {}).map(([language, content]) => (
                  <div key={language} className="mt-3">
                    <p className="text-xs font-medium">Translation · {language}</p>
                    <p className="whitespace-pre-wrap" lang={language}>{content.edited_translation || content.translated_markdown}</p>
                  </div>
                ))}
              </details>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
