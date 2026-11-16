import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { apiFetch } from '../auth/keycloak'
import { API_BASE } from '../config'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'

export default function WebhookSourceActivity({ path, enabled }) {
  const id = useId()
  const [deliveries, setDeliveries] = useState(null)
  const [secret, setSecret] = useState('')
  const [token, setToken] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function perform(action) {
    setError('')
    setBusy(true)
    try {
      await action()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  function generate(event) {
    event.preventDefault()
    const clientSecret = secret
    setSecret('')
    setToken(null)
    return perform(async () => setToken(await fetchJson(`${path}/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_secret: clientSecret }),
    })))
  }

  function downloadOriginal(key) {
    return perform(async () => {
      const response = await apiFetch(`${API_BASE}${path}/deliveries/original?delivery_key=${encodeURIComponent(key)}`)
      if (!response.ok) throw new Error('Could not download the original JSON.')
      const href = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = href
      link.download = 'webhook-original.json'
      link.click()
      window.setTimeout(() => URL.revokeObjectURL(href), 1000)
    })
  }

  return (
    <section className="space-y-3 rounded-md border border-border p-3" aria-label="Source activity and access">
      <Button variant="outline" size="sm" disabled={busy} onClick={() =>
        perform(async () => setDeliveries((await fetchJson(`${path}/deliveries`)).deliveries))}>
        {deliveries ? 'Refresh deliveries' : 'Show recent deliveries'}
      </Button>
      {deliveries?.length === 0 && <p className="text-sm text-muted-foreground">No accepted submissions yet.</p>}
      {deliveries?.length > 0 && (
        <ul className="divide-y divide-border text-sm">
          {deliveries.map(delivery => (
            <li key={delivery.idempotency_key} className="space-y-1 py-2">
              <p className="break-all">{delivery.idempotency_key} · Mapping {delivery.version} · {formatCompactDateTime(delivery.created_at)}</p>
              {delivery.has_original && (
                <Button type="button" variant="outline" size="sm" disabled={busy}
                  onClick={() => downloadOriginal(delivery.idempotency_key)}>Download original JSON</Button>
              )}
              {delivery.documents.map(document => (
                <p key={document.workflow_id}>
                  <Link className="underline" to={`/documents/${encodeURIComponent(document.workflow_id)}?tab=chunks`}>
                    {document.title || 'Open document'}
                  </Link>
                  {' · '}{document.stage || document.status}
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}
      {enabled && (
        <form className="space-y-2" onSubmit={generate} autoComplete="off">
          <label className="block text-sm" htmlFor={`${id}-secret`}>Existing service client secret</label>
          <div className="flex flex-wrap gap-2">
            <Input id={`${id}-secret`} className="max-w-sm" type="password" value={secret} required maxLength={4096}
              onChange={event => setSecret(event.target.value)} disabled={busy} autoComplete="off" />
            <Button variant="outline" size="sm" disabled={busy || !secret}>Generate service token</Button>
          </div>
          <p className="text-xs text-muted-foreground">Ask your operator for the service client secret. The app does not save the secret or token.</p>
        </form>
      )}
      {token && (
        <div className="space-y-2">
          <label className="block text-sm" htmlFor={`${id}-token`}>Access token · {token.expires_in} seconds</label>
          <Input id={`${id}-token`} readOnly value={token.access_token} onFocus={event => event.target.select()} />
          <Button variant="outline" size="sm" onClick={() => setToken(null)}>Clear token</Button>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </section>
  )
}
