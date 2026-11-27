import { useEffect, useState } from 'react'
import { fetchJson } from '../lib/pipelineUi'
import { Button } from './ui/button'
import MetadataEditor from './MetadataEditor'

const metadataLabels = { category: 'Category', crop: 'Crop', season: 'Season', location: 'Applicable location' }

function Fact({ fact }) {
  return (
    <span className="min-w-0">
      <span className="block break-words text-sm"><strong>{metadataLabels[fact.field]}</strong> · {fact.value}</span>
      <span className="block break-words text-xs text-muted-foreground">{fact.evidence ? `“${fact.evidence.quote}”` : 'Entered by a reviewer'}</span>
      {fact.field === 'location' && !fact.area && (
        <span className="block text-xs text-muted-foreground">Area code unavailable. This name is saved for reference.</span>
      )}
    </span>
  )
}

export default function MetadataReview({ workflowId, chunkNumber, canEdit, onSaved }) {
  const [open, setOpen] = useState(false)
  const [review, setReview] = useState(null)
  const [selected, setSelected] = useState([])
  const [busy, setBusy] = useState('')
  const [editingSuggestions, setEditingSuggestions] = useState(false)
  const [editorRevision, setEditorRevision] = useState(0)
  const [notice, setNotice] = useState('')
  const path = `/documents/${encodeURIComponent(workflowId)}/metadata-review`
  const params = chunkNumber == null ? '' : `?chunk_number=${chunkNumber}`
  const editable = canEdit && Boolean(review?.version)
  const fallbackFacts = (review?.inherited || review?.supplied || []).filter(fact => {
    const fields = review?.accepted?.overrides || (chunkNumber == null ? [] : review?.accepted?.facts.map(item => item.field)) || []
    return !fields.includes(fact.field)
  })

  useEffect(() => {
    if (!open) return
    const request = new AbortController()
    fetchJson(path + params, { signal: request.signal }).then(value => {
      if (request.signal.aborted) return
      setReview(value)
      setSelected(value.pending?.facts.map((_, index) => index) || [])
    }).catch(error => {
      if (!request.signal.aborted) setNotice(error.message)
    })
    return () => request.abort()
  }, [open, path, params])

  async function change(action, body) {
    setBusy(action)
    setNotice('')
    try {
      const options = { method: 'POST' }
      if (action === 'accept') {
        options.headers = { 'Content-Type': 'application/json' }
        options.body = JSON.stringify({ proposal_id: review.pending.id, selected })
      }
      if (action === 'save') {
        options.method = 'PUT'
        options.headers = { 'Content-Type': 'application/json' }
        options.body = JSON.stringify(body)
      }
      const target = action === 'save' ? path : `${path}/${action}`
      const value = await fetchJson(target + params, options)
      setReview(current => ({
        ...value, supplied: value.supplied ?? current?.supplied, inherited: value.inherited ?? current?.inherited,
        category_options: value.category_options ?? current?.category_options,
        excluded_areas: value.excluded_areas ?? current?.excluded_areas,
      }))
      setSelected(value.pending?.facts.map((_, index) => index) || [])
      if (action === 'accept' || action === 'save') {
        setEditingSuggestions(false)
        setEditorRevision(current => current + 1)
        setNotice('Metadata saved. Re-ingest to update search.')
        onSaved?.()
      }
    } catch (error) {
      setNotice(error.message)
    } finally {
      setBusy('')
    }
  }

  return (
    <details className="rounded-lg border border-border px-3" onToggle={event => {
      setOpen(event.currentTarget.open)
      if (!event.currentTarget.open) {
        setReview(null)
        setEditingSuggestions(false)
      }
    }}>
      <summary className="cursor-pointer py-2 text-sm font-medium">
        {chunkNumber == null ? 'Document metadata' : 'Chunk metadata'}
      </summary>
      {open && (
        <div className="space-y-3 pb-3">
          {notice && <p role="status" className="break-words text-sm text-muted-foreground">{notice}</p>}
          {review == null && !notice && <p role="status" className="text-sm text-muted-foreground">Loading metadata…</p>}
          {!editable && review?.accepted?.facts.length > 0 && (
            <ul className="space-y-2" aria-label="Accepted metadata">
              {review.accepted.facts.map((fact, index) => <li key={index}><Fact fact={fact} /></li>)}
            </ul>
          )}
          {!editable && chunkNumber == null && review?.validity && (
            <dl className="grid max-w-md grid-cols-2 gap-2 text-sm" aria-label="Document validity">
              <div><dt className="font-medium">Start date</dt><dd className="text-muted-foreground">{review.validity_days?.valid_from || 'Unspecified'}</dd></div>
              <div><dt className="font-medium">End date</dt><dd className="text-muted-foreground">{review.validity_days?.valid_to || 'No expiry'}</dd></div>
            </dl>
          )}
          {!editable && fallbackFacts.length > 0 && (
            <ul className="space-y-1 text-sm" aria-label={chunkNumber == null ? 'Supplied metadata' : 'Inherited metadata'}>
              {fallbackFacts.map((fact, index) => (
                <li className="break-words text-muted-foreground" key={index}>{metadataLabels[fact.field]} · {fact.value}</li>
              ))}
            </ul>
          )}
          {canEdit && !editingSuggestions && review?.pending && (
            <fieldset className="space-y-2">
              <legend className="mb-2 text-sm font-medium">Suggested metadata</legend>
              {review.pending.facts.length === 0 && <p className="text-sm text-muted-foreground">No supported fields found.</p>}
              {review.pending.facts.map((fact, index) => (
                <label key={index} className="flex items-start gap-2">
                  <input type="checkbox" className="mt-1 shrink-0 accent-primary" checked={selected.includes(index)}
                    disabled={Boolean(busy)} onChange={event => setSelected(current => event.target.checked
                      ? [...current, index]
                      : current.filter(item => item !== index))} />
                  <Fact fact={fact} />
                </label>
              ))}
            </fieldset>
          )}
          {editable && (
            <MetadataEditor key={`${workflowId}:${chunkNumber}:${editorRevision}:${editingSuggestions}`}
              review={review} labels={metadataLabels} useSuggestions={editingSuggestions} isChunk={chunkNumber != null}
              saving={busy === 'save'} busy={Boolean(busy)} onSave={body => change('save', body)}
              onCancel={editingSuggestions ? () => setEditingSuggestions(false) : undefined} />
          )}
          {canEdit && !editingSuggestions && (
            <div className="flex flex-wrap gap-2">
              {editable && review?.pending?.facts.length > 0 && (
                <Button type="button" size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => setEditingSuggestions(true)}>
                  Edit suggestions
                </Button>
              )}
              <Button type="button" size="sm" variant="outline" disabled={Boolean(busy) || review == null} onClick={() => change('generate')}>
                {busy === 'generate' ? 'Generating…' : 'Generate metadata'}
              </Button>
              {review?.pending?.facts.length > 0 && (
                <Button type="button" size="sm" disabled={Boolean(busy) || selected.length === 0} onClick={() => change('accept')}>
                  {busy === 'accept' ? 'Saving…' : 'Accept selected'}
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </details>
  )
}
