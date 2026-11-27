import { useId, useState } from 'react'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Textarea } from '../components/ui/textarea'
import { fetchJson } from '../lib/pipelineUi'

export default function IntakeTemplateEditor({ provider, templates, disabled, onSave }) {
  const id = useId()
  const [name, setName] = useState('')
  const [expression, setExpression] = useState('')
  const [sample, setSample] = useState('')
  const [output, setOutput] = useState(null)
  const [error, setError] = useState('')
  const [testing, setTesting] = useState(false)
  const mapping = { runtime: 'jsonata-v1', expression: expression.trim() }
  const validName = /^[a-z][a-z0-9-]{0,63}$/.test(name)

  async function preview() {
    setOutput(null)
    setError('')
    setTesting(true)
    try {
      const result = await fetchJson(`/knowledge/intake/${encodeURIComponent(provider)}/mapping-preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mapping, fixture: JSON.parse(sample) }),
      })
      setOutput(result.documents)
    } catch (failure) {
      setError(failure instanceof SyntaxError ? 'Enter valid sample JSON.' : failure.message)
    } finally {
      setTesting(false)
    }
  }

  return (
    <details>
      <summary className="cursor-pointer text-sm">Create or edit a JSONata template</summary>
      <fieldset disabled={disabled || testing} className="mt-3 space-y-3">
        <div className="flex flex-wrap gap-2">
          {Object.entries(templates).filter(([, mapping]) => mapping.runtime === 'jsonata-v1').map(([key, template]) => (
            <Button key={key} type="button" size="sm" variant="outline" onClick={() => {
              setName(key)
              setExpression(template.expression)
              setOutput(null)
              setError('')
            }}>{key}</Button>
          ))}
        </div>
        <label className="block text-sm" htmlFor={`${id}-name`}>Template name</label>
        <Input id={`${id}-name`} value={name} maxLength={64} placeholder="daily-advisories"
          onChange={event => setName(event.target.value)} />
        <label className="block text-sm" htmlFor={`${id}-expression`}>JSONata expression</label>
        <Textarea id={`${id}-expression`} value={expression} rows={8} maxLength={8192} className="font-mono"
          onChange={event => { setExpression(event.target.value); setOutput(null) }} />
        <label className="block text-sm" htmlFor={`${id}-sample`}>Sample input JSON</label>
        <Textarea id={`${id}-sample`} value={sample} rows={8} className="font-mono"
          onChange={event => { setSample(event.target.value); setOutput(null) }} />
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={!mapping.expression || !sample.trim()}
            onClick={preview}>{testing ? 'Running…' : 'Run preview'}</Button>
          <Button type="button" disabled={!validName || !output?.length}
            onClick={() => onSave(name, mapping)}>Save template</Button>
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {output && (
          <section aria-label="Canonical JSON output" className="space-y-1">
            <p role="status" className="text-sm">{output.length} {output.length === 1 ? 'document' : 'documents'} mapped</p>
            <pre className="max-h-80 overflow-auto rounded-md border border-border p-2 text-xs">{JSON.stringify(output, null, 2)}</pre>
          </section>
        )}
      </fieldset>
    </details>
  )
}
