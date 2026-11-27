import { useEffect, useId, useState } from 'react'
import { Button } from '../components/ui/button'
import { Textarea } from '../components/ui/textarea'
import { fetchJson } from '../lib/pipelineUi'
import IntakeTemplateEditor from './IntakeTemplateEditor'

export default function ProviderSourceSettings({ provider, onSaved }) {
  const id = useId()
  const [version, setVersion] = useState(null)
  const [draft, setDraft] = useState('')
  const [templates, setTemplates] = useState({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const path = `/providers/${encodeURIComponent(provider)}/source-settings`

  function apply(result) {
    const { version: revision, ...settings } = result
    setVersion(revision)
    setTemplates(settings.templates || {})
    setDraft(JSON.stringify(settings, null, 2))
  }

  useEffect(() => {
    const controller = new AbortController()
    setVersion(null)
    setError('')
    setNotice('')
    fetchJson(path, { signal: controller.signal }).then(apply).catch(failure => {
      if (failure.name !== 'AbortError') setError(failure.message)
    })
    return () => controller.abort()
  }, [path])

  async function save(template) {
    setError('')
    setNotice('')
    setBusy(true)
    try {
      const settings = JSON.parse(draft)
      if (template) settings.templates = { ...settings.templates, [template.name]: template.mapping }
      const result = await fetchJson(path, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...settings, version }),
      })
      apply(result)
      onSaved?.()
      setNotice(template ? 'Template saved. Select it in Webhook intake and pin the source mapping.'
        : 'Source settings saved. Scheduled sources use the new settings on their next check.')
    } catch (failure) {
      setError(failure instanceof SyntaxError ? 'Enter valid source settings JSON.' : failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-labelledby={`${id}-title`} className="space-y-3 border-t border-border pt-4">
      <h3 id={`${id}-title`} className="text-sm font-semibold">URL sources and intake templates</h3>
      <p className="text-sm text-muted-foreground">Public URLs can be submitted directly. Save a source when it needs a named template, an approved sign-in policy, or a recurring schedule.</p>
      <IntakeTemplateEditor provider={provider} templates={templates} disabled={busy || version === null}
        onSave={(name, mapping) => save({ name, mapping })} />
      <details>
        <summary className="cursor-pointer text-sm">Edit source settings</summary>
        <form className="mt-3 space-y-3" onSubmit={event => { event.preventDefault(); save() }}>
          <p className="text-xs text-muted-foreground">Templates map incoming JSON. Crawl sources name a host and optional intake_template. Schedules also need source_key, url, and interval_seconds (60–86400). Existing authenticated sources keep their operator-managed credentials and host.</p>
          <label className="block text-sm" htmlFor={`${id}-settings`}>Source settings JSON</label>
          <Textarea id={`${id}-settings`} rows={18} className="font-mono" value={draft}
            disabled={busy || version === null} onChange={event => setDraft(event.target.value)} />
          <Button disabled={busy || version === null}>{busy ? 'Saving…' : 'Save source settings'}</Button>
        </form>
      </details>
      {notice && <p role="status" className="text-sm">{notice}</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </section>
  )
}
