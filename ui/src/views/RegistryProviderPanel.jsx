import { useEffect, useId, useRef, useState } from 'react'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { fetchJson } from '../lib/pipelineUi'

const selectStyle = 'h-9 w-full rounded-md border border-input bg-background px-3 text-sm'

function Field({ label, children, ...props }) {
  const id = useId()
  return <div className="min-w-0 space-y-1.5">
    <Label htmlFor={id}>{label}</Label>
    {children ? <select id={id} className={selectStyle} {...props}>{children}</select> : <Input id={id} {...props} />}
  </div>
}

function ProposalStatus({ proposal }) {
  if (!proposal) return <p className="text-sm text-muted-foreground">No local proposal yet.</p>
  return <div className="space-y-1 text-sm">
    <p>Local proposal {proposal.version}: {proposal.decision?.outcome || 'awaiting operator decision'}.</p>
    {proposal.decision?.reason && <p className="text-muted-foreground">Decision reason: {proposal.decision.reason}</p>}
  </div>
}

function ProposalForm({ endpoint, snapshot, buildChange, onSubmitted, children, label }) {
  const pending = useRef(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function submit(event) {
    event.preventDefault()
    if (busy) return
    const fields = new FormData(event.currentTarget)
    const current = snapshot.current
    const body = {
      expected_version: snapshot.expected_version,
      change: buildChange(fields),
      observed_record: current ? { record_id: current.record_id, revision: current.revision } : null,
      reason: String(fields.get('reason')).trim(),
    }
    const signature = JSON.stringify(body)
    if (pending.current?.signature !== signature) pending.current = { signature, requestId: crypto.randomUUID() }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const proposal = await fetchJson(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, request_id: pending.current.requestId }),
      })
      pending.current = null
      onSubmitted(proposal)
      setNotice('Proposal submitted for operator review. The live Registry is unchanged.')
    } catch (failure) {
      setError(`${failure.message} Retry unchanged details to reuse this request. If the version changed, refresh Registry records.`)
    } finally {
      setBusy(false)
    }
  }

  return <form onSubmit={submit} className="space-y-3" aria-label={label}>
    <fieldset disabled={busy} className="grid min-w-0 gap-3 md:grid-cols-2">
      {children}
      <Field label="Requested status" name="status" defaultValue={snapshot.latest_proposal?.change?.status || snapshot.current?.status || 'active'}>
        <option value="active">Active</option>
        <option value="inactive" disabled={!snapshot.current}>Inactive</option>
      </Field>
      <Field label="Reason for change" name="reason" required maxLength={1000} />
    </fieldset>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {notice && <p role="status" className="text-sm text-muted-foreground">{notice}</p>}
    <Button type="submit" variant="outline" disabled={busy}>{busy ? 'Submitting…' : label}</Button>
  </form>
}

function AdmissionEditor({ context, endpoint, onSubmitted }) {
  const snapshot = context.admission
  const initial = snapshot.latest_proposal?.change || snapshot.current || {}
  return <section className="space-y-3" aria-label="Provider admission">
    <h4 className="text-sm font-semibold">Provider admission</h4>
    <p className="text-sm">Live Registry: {snapshot.current ? snapshot.current.status : 'not registered'}.</p>
    {snapshot.current && <p className="break-all text-sm text-muted-foreground">{snapshot.current.name} · {snapshot.current.base_url}</p>}
    <ProposalStatus proposal={snapshot.latest_proposal} />
    <ProposalForm label="Submit admission proposal" endpoint={endpoint} snapshot={snapshot} onSubmitted={onSubmitted}
      buildChange={fields => ({ name: fields.get('name'), base_url: fields.get('base_url'), status: fields.get('status') })}>
      <Field label="Participant name" name="name" defaultValue={initial.name || ''} required maxLength={200} />
      <Field label="Provider base URL" name="base_url" type="url" defaultValue={initial.base_url || ''} required maxLength={2000} />
    </ProposalForm>
  </section>
}

function RouteEditor({ context, endpoint, capability, onSubmitted }) {
  const snapshot = context.route
  const proposed = snapshot.latest_proposal?.change
  const initial = proposed?.route || snapshot.current?.actions?.[0] || {}
  const packs = context.schema_packs.filter(pack => pack.capability === capability)
  return <section className="space-y-3 border-t border-border pt-4" aria-label="Provider route">
    <h4 className="text-sm font-semibold">Provider route</h4>
    <p className="text-sm">Live route: {snapshot.current ? snapshot.current.status : 'not registered'}.</p>
    {snapshot.current?.actions?.length > 0 && <ul className="space-y-1 text-sm text-muted-foreground">
      {snapshot.current.actions.map(action => <li key={action.action} className="break-all">{action.action}: {action.method} {action.path} · {action.status}</li>)}
    </ul>}
    <ProposalStatus proposal={snapshot.latest_proposal} />
    {packs.length === 0 ? <p className="text-sm text-muted-foreground">Install a schema pack for this capability before proposing a route.</p> :
      <ProposalForm label="Submit route proposal" endpoint={endpoint} snapshot={snapshot} onSubmitted={onSubmitted}
        buildChange={fields => ({
          schema_pack_digest: fields.get('schema_pack_digest'), status: fields.get('status'),
          route: {
            network_id: context.network_id, participant_id: context.participant_id, capability,
            ...Object.fromEntries(['action', 'base_url', 'method', 'path', 'mapping_url', 'mapping_sha256', 'mapping_runtime', 'auth_profile'].map(name => [name, fields.get(name)])),
            timeout_ms: Number(fields.get('timeout_ms')), retry_max: Number(fields.get('retry_max')),
          },
        })}>
        <Field label="Schema pack" name="schema_pack_digest" defaultValue={proposed?.schema_pack_digest || packs[0].digest}>
          {packs.map(pack => <option key={pack.digest} value={pack.digest}>{pack.version} · {pack.digest.slice(0, 12)}</option>)}
        </Field>
        <Field label="Action" name="action" defaultValue={initial.action || 'select'}>
          {['discover', 'select', 'init', 'confirm', 'status', 'track', 'cancel', 'update', 'rate', 'support'].map(action => <option key={action}>{action}</option>)}
        </Field>
        <Field label="Route base URL" name="base_url" type="url" defaultValue={initial.base_url || context.admission.current?.base_url || ''} required maxLength={2000} />
        <Field label="HTTP method" name="method" defaultValue={initial.method || 'POST'}><option>POST</option><option>GET</option></Field>
        <Field label="Request path" name="path" defaultValue={initial.path || '/search'} required maxLength={512} />
        <Field label="Mapping URL" name="mapping_url" type="url" defaultValue={initial.mapping_url || ''} required maxLength={2000} />
        <Field label="Mapping SHA-256" name="mapping_sha256" defaultValue={initial.mapping_sha256 || ''} required pattern="[0-9a-f]{64}" />
        <Field label="Mapping runtime version" name="mapping_runtime" defaultValue={initial.mapping_runtime || ''} placeholder="jsonata@1.5.4" required />
        <Field label="Authentication profile" name="auth_profile" defaultValue={initial.auth_profile || ''} required />
        <Field label="Timeout (milliseconds)" name="timeout_ms" type="number" min={1000} max={120000} defaultValue={initial.timeout_ms ?? 15000} required />
        <Field label="Maximum retries" name="retry_max" type="number" min={0} max={5} defaultValue={initial.retry_max ?? 0} required />
      </ProposalForm>}
  </section>
}

function RegistryContext({ provider, networkId, capability, onCapabilities }) {
  const [context, setContext] = useState(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const base = `/providers/${encodeURIComponent(provider)}/networks/${encodeURIComponent(networkId)}`
  useEffect(() => {
    let active = true
    setContext(null)
    setError('')
    fetchJson(`${base}/registry-context${capability ? `?capability=${encodeURIComponent(capability)}` : ''}`)
      .then(result => { if (active) { setContext(result); onCapabilities(result.schema_packs) } })
      .catch(failure => { if (active) setError(failure.message) })
    return () => { active = false }
  }, [base, capability, revision, onCapabilities])

  function submitted(kind, proposal) {
    setContext(current => {
      if (!current || proposal.version < current[kind].expected_version) return current
      return { ...current, [kind]: { ...current[kind], expected_version: proposal.version, latest_proposal: proposal } }
    })
  }
  return <div className="space-y-4">
    <Button type="button" variant="outline" size="sm" onClick={() => setRevision(value => value + 1)}>Refresh Registry records</Button>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {!context && !error && <p role="status" className="text-sm text-muted-foreground">Reading Registry records…</p>}
    {context && <>
      <p className="break-all text-sm text-muted-foreground">Participant: {context.participant_id}</p>
      <AdmissionEditor context={context} endpoint={`${base}/registry-proposals`} onSubmitted={proposal => submitted('admission', proposal)} />
      {capability && context.route && <RouteEditor context={context} capability={capability} endpoint={`${base}/registry-proposals`} onSubmitted={proposal => submitted('route', proposal)} />}
    </>}
  </div>
}

function NetworkEditor({ provider, networkId }) {
  const [packs, setPacks] = useState([])
  const [capability, setCapability] = useState('')
  return <div className="space-y-4">
    <Field label="Route capability" value={capability} onChange={event => setCapability(event.target.value)}>
      <option value="">Admission only</option>
      {[...new Set(packs.map(pack => pack.capability))].map(value => <option key={value}>{value}</option>)}
    </Field>
    <RegistryContext key={capability} provider={provider} networkId={networkId} capability={capability} onCapabilities={setPacks} />
  </div>
}

export default function RegistryProviderPanel({ provider }) {
  const [networks, setNetworks] = useState(null)
  const [networkId, setNetworkId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function loadNetworks() {
    setBusy(true)
    setError('')
    try {
      const result = await fetchJson(`/providers/${encodeURIComponent(provider)}/registry-networks`)
      setNetworks(result.networks)
      setNetworkId(result.networks[0]?.network_id || '')
    } catch (failure) { setError(failure.message) }
    finally { setBusy(false) }
  }
  return <section aria-label="Registry onboarding" className="space-y-4 border-t border-border pt-4">
    <h3 className="text-sm font-semibold">Registry onboarding</h3>
    <p className="text-sm text-muted-foreground">Review live network records and propose provider admission or route changes. Operator decisions are local records; they do not update the live Registry.</p>
    {networks === null && <Button type="button" variant="outline" onClick={loadNetworks} disabled={busy}>{busy ? 'Loading connections…' : 'Load Registry connections'}</Button>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {networks?.length === 0 && <p className="text-sm text-muted-foreground">No Registry connections configured for this instance.</p>}
    {networks?.length > 0 && <>
      <Field label="Registry network" value={networkId} onChange={event => setNetworkId(event.target.value)}>
        {networks.map(network => <option key={network.network_id} value={network.network_id}>{network.network_id}</option>)}
      </Field>
      <NetworkEditor key={networkId} provider={provider} networkId={networkId} />
    </>}
  </section>
}
