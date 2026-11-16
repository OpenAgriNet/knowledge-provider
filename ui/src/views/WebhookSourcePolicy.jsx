import { useEffect, useId, useState } from 'react'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Textarea } from '../components/ui/textarea'
import { API_BASE } from '../config'
import { fetchJson } from '../lib/pipelineUi'
import WebhookSourceActivity from './WebhookSourceActivity'

const SOURCE_KEY = /^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$/
const DEFAULT_MAPPING = { fields: { external_id: '/id', title: '/title', text: '/text' } }
const FIELD_PATHS = {
  external_id: ['/id', '/externalId', '/advisoryId', '/external_id'],
  title: ['/title', '/name'],
  text: ['/text', '/content/text', '/body', '/content'],
  language: ['/language', '/content/language'],
  translations: ['/translations', '/content/translations'],
  kind: ['/kind'],
  source_url: ['/sourceUrl', '/source_url'],
  crop_subjects: ['/cropSubjects', '/crop_subjects', '/applicability/cropSubjects'],
  knowledge_metadata: ['/metadata', '/knowledge_metadata'],
  valid_from: ['/validFrom', '/valid_from', '/applicability/validFrom'],
  valid_to: ['/validTo', '/valid_to', '/applicability/validTo'],
}

function parseObject(text, label, allowArray = false) {
  let value
  try {
    value = JSON.parse(text)
  } catch {
    throw new Error(`${label} must be valid JSON.`)
  }
  if (value === null || typeof value !== 'object' || (!allowArray && Array.isArray(value))) {
    throw new Error(`${label} must be a JSON ${allowArray ? 'object or array' : 'object'}.`)
  }
  return value
}

function suggestMapping(fixture) {
  const collection = ['records', 'items', 'events', 'advisories'].find(key => Array.isArray(fixture[key]))
  const record = collection ? fixture[collection][0] : Array.isArray(fixture) ? fixture[0] : fixture
  const fields = {}
  for (const [field, paths] of Object.entries(FIELD_PATHS)) {
    const selected = paths.find(path => {
      const value = path.slice(1).split('/').reduce((parent, key) => parent?.[key], record)
      if (field === 'external_id') return typeof value === 'string' || Number.isInteger(value)
      if (field === 'crop_subjects') return Array.isArray(value)
      if (field === 'translations' || field === 'knowledge_metadata') {
        return value != null && typeof value === 'object' && !Array.isArray(value)
      }
      return typeof value === 'string' && Boolean(value.trim())
    })
    if (selected) fields[field] = selected
  }
  if (!fields.external_id || !fields.title || !fields.text) {
    throw new Error('Could not find an ID, title and text in the first sample record. Enter the paths in Mapping JSON.')
  }
  return { ...(collection ? { records: `/${collection}` } : {}), fields }
}

function draftFrom(source, policy = {}) {
  return {
    source,
    version: policy.version ?? 0,
    serviceClient: policy.service_client ?? '',
    serviceSubject: policy.service_subject ?? '',
    mappingMode: policy.mapping?.runtime === 'canonical-v1' ? 'canonical' : policy.mapping?.runtime === 'jsonata-v1' ? 'jsonata' : 'fields',
    expression: policy.mapping?.expression ?? '',
    mapping: JSON.stringify(policy.mapping ?? DEFAULT_MAPPING, null, 2),
    template: '',
    fixture: '',
    enabled: policy.enabled ?? true,
    autoRelease: policy.auto_release ?? false,
    releaseNetworkId: policy.release_network_id ?? '',
  }
}

export default function WebhookSourcePolicy({ provider, sourceRevision }) {
  const id = useId()
  const [sources, setSources] = useState([])
  const [templates, setTemplates] = useState([])
  const [indexOnly, setIndexOnly] = useState(false)
  const [source, setSource] = useState('')
  const [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const pathFor = key => `/knowledge/intake/${encodeURIComponent(provider)}/${encodeURIComponent(key)}`
  const selectedSource = source.trim()
  const directory = `/knowledge/intake/${encodeURIComponent(provider)}`

  useEffect(() => {
    const controller = new AbortController()
    fetchJson(directory, { signal: controller.signal }).then(result => {
      setSources(result.sources)
      setIndexOnly(result.index_only)
    }).catch(failure => {
      if (failure.name !== 'AbortError') setError(failure.message)
    })
    return () => controller.abort()
  }, [directory])

  useEffect(() => {
    const controller = new AbortController()
    setTemplates([])
    fetchJson(`/providers/${encodeURIComponent(provider)}/source-settings`, { signal: controller.signal })
      .then(result => {
        if (!controller.signal.aborted) setTemplates(Object.keys(result.templates ?? {}).sort())
      })
      .catch(() => {})
    return () => controller.abort()
  }, [provider, sourceRevision])

  async function open(existing, key = selectedSource) {
    setError('')
    setNotice('')
    if (!SOURCE_KEY.test(key)) {
      setError('Use a source key such as public-notices. Start and end with a lowercase letter or number.')
      return
    }
    setBusy(true)
    try {
      const policy = existing ? await fetchJson(pathFor(key)) : undefined
      setDraft({ ...draftFrom(key, policy), savedEnabled: policy?.enabled })
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  function update(patch) {
    setNotice('')
    setDraft(current => ({ ...current, ...patch }))
  }

  function suggest() {
    setError('')
    try {
      const fixture = parseObject(draft.fixture, 'Sample submission', true)
      update({ mapping: JSON.stringify(suggestMapping(fixture), null, 2) })
    } catch (failure) {
      setError(failure.message)
    }
  }

  async function save(event) {
    event.preventDefault()
    setError('')
    setNotice('')
    setBusy(true)
    try {
      const mapping = {}
      if (draft.mappingMode === 'fields') mapping.mapping = parseObject(draft.mapping, 'Mapping')
      if (draft.mappingMode === 'jsonata') mapping.mapping = { runtime: 'jsonata-v1', expression: draft.expression.trim() }
      if (draft.mappingMode === 'template') mapping.template = draft.template.trim()
      const result = await fetchJson(pathFor(draft.source), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_version: draft.version,
          service_client: draft.serviceClient.trim(),
          service_subject: draft.serviceSubject.trim() || null,
          ...mapping,
          fixture: parseObject(draft.fixture, 'Sample submission', true),
          enabled: draft.enabled,
          auto_release: draft.autoRelease,
          release_network_id: draft.autoRelease ? draft.releaseNetworkId.trim() || null : null,
        }),
      })
      setDraft({ ...draftFrom(draft.source, result), fixture: draft.fixture, savedEnabled: result.enabled })
      setSources(current => [
        ...current.filter(item => item.source_key !== draft.source),
        { source_key: draft.source, enabled: result.enabled, version: result.version },
      ].sort((a, b) => a.source_key.localeCompare(b.source_key)))
      setNotice(`Pinned mapping version ${result.version}.`)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby={`${id}-title`} className="space-y-3 border-t border-border pt-4">
      <h3 id={`${id}-title`} className="text-sm font-semibold">Webhook intake</h3>
      <p className="text-sm text-muted-foreground">Open a source to find its submission URL, generate a service token, or inspect deliveries.</p>
      {sources.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label="Configured sources">
          {sources.map(item => (
            <li key={item.source_key}>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => {
                setSource(item.source_key)
                open(true, item.source_key)
              }}>{item.source_key} · {item.enabled ? 'Active' : 'Paused'}</Button>
            </li>
          ))}
        </ul>
      )}
      <form className="flex flex-wrap items-end gap-2" onSubmit={event => { event.preventDefault(); open(true) }}>
        <div className="min-w-40 flex-1 space-y-1.5 sm:max-w-sm">
          <Label htmlFor={`${id}-source`}>Source key</Label>
          <Input id={`${id}-source`} value={source} maxLength={63} placeholder="public-notices" required disabled={busy}
            onChange={event => setSource(event.target.value)} />
        </div>
        <Button variant="outline" type="submit" disabled={busy || !selectedSource}>Load policy</Button>
        <Button variant="outline" type="button" disabled={busy || !selectedSource} onClick={() => open(false)}>New policy</Button>
      </form>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {notice && <p role="status" className="text-sm text-muted-foreground">{notice}</p>}
      {draft && (
        <form onSubmit={save} className="space-y-3 rounded-md border border-border p-3">
          <fieldset disabled={busy} className="min-w-0 space-y-3">
            <legend className="px-1 text-sm font-medium">{draft.source} · {draft.version ? `Version ${draft.version}` : 'New policy'}</legend>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-url`}>Submission URL</Label>
              <Input id={`${id}-url`} readOnly value={new URL(`${API_BASE}${pathFor(draft.source)}/submissions`, window.location.origin).href} />
              <p className="text-xs text-muted-foreground">Send a Keycloak service token and a unique Idempotency-Key header with each submission.</p>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor={`${id}-client`}>Keycloak service client ID</Label>
                <Input id={`${id}-client`} maxLength={256} required value={draft.serviceClient}
                  onChange={event => update({ serviceClient: event.target.value, serviceSubject: '' })} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${id}-subject`}>Service account subject (optional)</Label>
                <Input id={`${id}-subject`} maxLength={256} value={draft.serviceSubject}
                  onChange={event => update({ serviceSubject: event.target.value })} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${id}-fixture`}>Sample submission JSON</Label>
                <Textarea id={`${id}-fixture`} rows={8} className="font-mono" required value={draft.fixture}
                  placeholder={'{"external_id":"example-1","title":"Public notice","text":"Full notice text"}'}
                  onChange={event => update({ fixture: event.target.value })} />
                {draft.mappingMode === 'fields' && (
                  <Button type="button" variant="outline" size="sm" disabled={!draft.fixture.trim()} onClick={suggest}>Suggest field paths</Button>
                )}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${id}-mapping-mode`}>Payload format</Label>
                <select id={`${id}-mapping-mode`} className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={draft.mappingMode} onChange={event => update({ mappingMode: event.target.value })}>
                  <option value="canonical">Canonical knowledge JSON</option>
                  <option value="fields">Map field paths</option>
                  <option value="jsonata">Transform with JSONata</option>
                  <option value="template">Use a configured template</option>
                </select>
                {draft.mappingMode === 'canonical' && (
                  <p className="text-xs text-muted-foreground">Send an external ID, title, and the full text using the canonical field names.</p>
                )}
                {draft.mappingMode === 'fields' && (
                  <>
                    <Label htmlFor={`${id}-mapping`}>Mapping JSON</Label>
                    <Textarea id={`${id}-mapping`} rows={8} className="font-mono" required value={draft.mapping}
                      onChange={event => update({ mapping: event.target.value })} />
                    <p className="text-xs text-muted-foreground">Map a record ID, title and text using JSON Pointer paths.</p>
                  </>
                )}
                {draft.mappingMode === 'jsonata' && (
                  <>
                    <Label htmlFor={`${id}-expression`}>JSONata expression</Label>
                    <Textarea id={`${id}-expression`} rows={8} className="font-mono" required value={draft.expression}
                      onChange={event => update({ expression: event.target.value })} />
                    <p className="text-xs text-muted-foreground">Return a canonical knowledge object or an array of objects. The sample submission is checked before saving.</p>
                  </>
                )}
                {draft.mappingMode === 'template' && (
                  <>
                    <Label htmlFor={`${id}-template`}>Template name</Label>
                    <Input id={`${id}-template`} value={draft.template} maxLength={64} pattern="[a-z][a-z0-9\x2d]{0,63}" required
                      list={`${id}-templates`} onChange={event => update({ template: event.target.value })} />
                    <datalist id={`${id}-templates`}>
                      {templates.map(template => <option key={template} value={template} />)}
                    </datalist>
                  </>
                )}
              </div>
            </div>
            <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
              <label className="flex items-center gap-2"><input type="checkbox" checked={draft.enabled}
                onChange={event => update({ enabled: event.target.checked })} /> Accept submissions</label>
              {!indexOnly && <label className="flex items-center gap-2"><input type="checkbox" checked={draft.autoRelease}
                onChange={event => update({ autoRelease: event.target.checked })} /> Publish automatically</label>}
            </div>
            {!indexOnly && draft.autoRelease && (
              <div className="max-w-sm space-y-1.5">
                <Label htmlFor={`${id}-release-network`}>Release network</Label>
                <Input id={`${id}-release-network`} value={draft.releaseNetworkId} maxLength={253}
                  onChange={event => update({ releaseNetworkId: event.target.value })} />
                <p className="text-xs text-muted-foreground">Leave blank to use the deployment default.</p>
              </div>
            )}
            <Button type="submit" disabled={busy || !draft.serviceClient.trim()}>Pin mapping</Button>
          </fieldset>
        </form>
      )}
      {draft?.version > 0 && (
        <WebhookSourceActivity key={`${draft.source}:${draft.version}`} path={pathFor(draft.source)} enabled={draft.savedEnabled} />
      )}
    </section>
  )
}
