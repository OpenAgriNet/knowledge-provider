import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { fetchJson } from '../lib/pipelineUi'
import { IntakeMetadataFields, useIntakeMetadata } from './IntakeMetadataFields'

export default function UrlIntakeForm({ provider, kind, onQueued }) {
  const metadata = useIntakeMetadata()
  const [sources, setSources] = useState(null)
  const [source, setSource] = useState('')
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState(null)
  const path = `/providers/${encodeURIComponent(provider)}/crawl`

  useEffect(() => {
    const controller = new AbortController()
    setSources(null)
    setSource('')
    setResults(null)
    setError('')
    fetchJson(path, { signal: controller.signal }).then(({ sources }) => {
      setSources(sources)
      setSource('')
    }).catch((failure) => {
      if (failure.name !== 'AbortError') setError(failure.message)
    })
    return () => controller.abort()
  }, [path])

  async function submit(event) {
    event.preventDefault()
    setError('')
    setResults(null)
    setBusy(true)
    try {
      const result = await fetchJson(source ? `${path}/${encodeURIComponent(source)}` : path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.trim(), kind, ...metadata.values() }),
      })
      setResults(result.items ?? [result])
      onQueued()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="panel space-y-3 p-4" onSubmit={submit}>
      <h2 className="text-sm font-semibold">Ingest from a URL</h2>
      <p className="text-xs text-muted-foreground">Paste a public HTTPS URL for a page, JSON feed, text, or supported file. Its original response is kept privately alongside the processed content.</p>
      <label className="block text-sm">
        Access
        <select className="mt-1 block h-9 w-full rounded-md border border-input bg-background px-3" value={source}
          disabled={busy} onChange={event => setSource(event.target.value)}>
          <option value="">Public URL, no sign-in required</option>
          {(sources ?? []).map(({ id, host }) => <option key={id} value={id}>{id} ({host})</option>)}
        </select>
      </label>
      {source && <p className="text-xs text-muted-foreground">This saved source uses its configured host and access policy.</p>}
      <label className="block text-sm">
        Source URL
        <Input type="url" value={url} maxLength={4096} required disabled={busy}
          onChange={event => setUrl(event.target.value)} placeholder="https://example.org/notice" />
      </label>
      <p className="text-xs text-muted-foreground">Use a direct link. Pages and JSON are limited to 2 MiB; supported files to 20 MiB. Redirects are not followed.</p>
      <IntakeMetadataFields state={metadata} disabled={busy} />
      <Button disabled={busy || !kind || !url.trim()}>{busy ? 'Fetching URL…' : 'Ingest URL'}</Button>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {results && (
        <div role="status" className="space-y-1 text-sm">
          {results.length === 0 && <p>No documents were found in the submission.</p>}
          {results.map(item => (
            <div key={item.workflow_id}>
              {item.status === 'start_pending' ? 'Saved; start pending.' : 'Submission received.'}{' '}
              <Link className="underline" to={`/documents/${encodeURIComponent(item.workflow_id)}`}>Open document</Link>
            </div>
          ))}
        </div>
      )}
    </form>
  )
}
