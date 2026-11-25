import { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { Button } from './ui/button'
import { Input } from './ui/input'

const suggestedSeasons = ['Kharif', 'Rabi', 'Navarai', 'Samba']

function distinctValues(values) {
  const labels = new Map()
  for (const value of values) {
    const label = value?.trim()
    if (label && !labels.has(label.toLowerCase())) labels.set(label.toLowerCase(), label)
  }
  return [...labels.values()]
}

function seedRows(facts) {
  const usedRules = new Set(facts.map(fact => fact.rule).filter(Boolean))
  const ruleByEvidence = new Map()
  let nextRule = 1
  return facts.map((fact, key) => {
    let rule = fact.rule
    if (!rule && fact.evidence && ['season', 'location'].includes(fact.field)) {
      const evidence = JSON.stringify(fact.evidence || null)
      if (!ruleByEvidence.has(evidence)) {
        while (usedRules.has(nextRule)) nextRule += 1
        ruleByEvidence.set(evidence, nextRule++)
      }
      rule = ruleByEvidence.get(evidence)
    }
    return { ...fact, key, rule }
  })
}

export default function MetadataEditor({ review, labels, useSuggestions, isChunk, saving, busy, onSave, onCancel }) {
  const suggestionId = useId()
  const saved = review.accepted?.facts || []
  const categoryOptions = review.category_options ?? []
  const owned = review.accepted?.overrides || saved.map(fact => fact.field)
  const supplied = (review.supplied || []).filter(fact => !owned.includes(fact.field))
  const current = [...saved, ...supplied.filter(fact => !saved.some(item =>
    item.field === fact.field && item.value.toLowerCase() === fact.value.toLowerCase()
      && item.rule === fact.rule
  ))]
  const proposed = useSuggestions ? review.pending.facts : []
  const proposedFields = new Set(proposed.map(fact => fact.field))
  const facts = useSuggestions ? [...current.filter(fact => !proposedFields.has(fact.field)), ...proposed] : current
  const [rows, setRows] = useState(() => seedRows(facts))
  const [editedFields, setEditedFields] = useState(proposedFields)
  const [clearedFields, setClearedFields] = useState(() => new Set(owned.filter(field => !saved.some(fact => fact.field === field))))
  const [nextKey, setNextKey] = useState(facts.length)
  const knownFacts = [...current, ...(review.pending?.facts || []), ...(review.inherited || [])]
  const [error, setError] = useState('')
  const initialStart = review.validity_days?.valid_from || ''
  const initialEnd = review.validity_days?.valid_to || ''
  const [validFrom, setValidFrom] = useState(initialStart)
  const [validTo, setValidTo] = useState(initialEnd)

  function markEdited(field) {
    setEditedFields(current => new Set([...current, field]))
  }

  function add(field) {
    markEdited(field)
    setClearedFields(current => new Set([...current].filter(item => item !== field)))
    setRows(current => [...current, { field, value: '', key: nextKey }])
    setNextKey(current => current + 1)
  }

  function update(row, patch) {
    markEdited(row.field)
    setRows(current => current.map(item => item.key === row.key ? { ...item, ...patch } : item))
  }

  function remove(row) {
    markEdited(row.field)
    setClearedFields(current => new Set([...current].filter(item => item !== row.field)))
    setRows(current => current.filter(item => item.key !== row.key))
  }

  function clearField(field, inherit) {
    markEdited(field)
    setRows(current => current.filter(item => item.field !== field))
    setClearedFields(current => new Set(inherit ? [...current].filter(item => item !== field) : [...current, field]))
  }

  function save(event) {
    event.preventDefault()
    const rowFields = new Set(rows.map(row => row.field))
    const overrides = Object.keys(labels).filter(field => {
      if (!editedFields.has(field)) return owned.includes(field)
      return !isChunk || rowFields.has(field) || clearedFields.has(field)
    })
    const kept = rows.filter(row => overrides.includes(row.field))
    if (kept.some(row => !row.value.trim())) {
      setError('Enter a value or remove the empty row.')
      return
    }
    if (kept.some(row => Boolean(row.area?.codeScheme?.trim()) !== Boolean(row.area?.areaCode?.trim()))) {
      setError('Enter both the area code scheme and area code, or leave both empty.')
      return
    }
    if (!isChunk && validFrom && validTo && validTo < validFrom) {
      setError('End date cannot be before the start date.')
      return
    }
    setError('')
    onSave({
      version: review.version,
      overrides,
      ...(!isChunk && (validFrom !== initialStart || validTo !== initialEnd) ? {
        validity: validFrom || validTo ? {
          ...(validFrom ? { startsAt: validFrom === initialStart ? review.validity.startsAt : validFrom } : {}),
          ...(validTo ? { endsAt: validTo === initialEnd ? review.validity.endsAt : validTo } : {}),
        } : null,
      } : {}),
      facts: kept.map(row => ({
        field: row.field,
        value: row.value.trim(),
        ...(row.area?.areaCode?.trim() ? { area: {
          ...row.area, codeScheme: row.area.codeScheme.trim(), areaCode: row.area.areaCode.trim(),
        } } : {}),
        ...(row.rule ? { rule: Number(row.rule) } : {}),
        ...(row.evidence ? { evidence: row.evidence } : {}),
      })),
    })
  }

  return (
    <form className="space-y-4" onSubmit={save}>
      {isChunk && <p className="text-sm text-muted-foreground">Chunk values take precedence in search. Use the document value, or clear a field for this chunk.</p>}

      {Object.keys(labels).map(field => {
        const label = labels[field]
        const fieldRows = rows.filter(row => row.field === field)
        const options = distinctValues([
          ...(field === 'season' ? suggestedSeasons : []),
          ...knownFacts.filter(fact => fact.field === field).map(fact => fact.value),
        ])
        const listId = `${suggestionId}-${field}`
        const hasCategoryEnum = field === 'category' && categoryOptions.length > 0
        return (
          <fieldset className="min-w-0 space-y-2 border-b border-border pb-3" key={field} disabled={busy}>
            <legend className="mb-1 text-sm font-semibold">{label}</legend>
            {fieldRows.length === 0 && <p className="text-xs text-muted-foreground">{isChunk && clearedFields.has(field) ? 'Cleared for this chunk' : 'No value recorded'}</p>}
            {options.length > 0 && !hasCategoryEnum && (
              <datalist id={listId}>
                {options.map(option => <option key={option} value={option} />)}
              </datalist>
            )}
            {fieldRows.map((row, index) => (
              <div key={row.key} className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  {hasCategoryEnum ? (
                    <div className="relative min-w-0 basis-40 flex-1">
                      <select aria-label={`${label} ${index + 1}`} className="h-9 w-full appearance-none rounded-md border border-input bg-background ps-3 pe-9 text-sm"
                        value={row.value} onChange={event => update(row, { value: event.target.value })}>
                        <option value="" disabled>Choose a category</option>
                        {[...new Set([...categoryOptions, ...(row.value ? [row.value] : [])])].map(option => (
                          <option key={option} value={option}>{option}</option>
                        ))}
                      </select>
                      <ChevronDown aria-hidden="true" className="pointer-events-none absolute end-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                    </div>
                  ) : (
                    <Input list={options.length ? listId : undefined} aria-label={`${label} ${index + 1}`} className="min-w-0 basis-40 flex-1" maxLength={128}
                      value={row.value} onChange={event => update(row, { value: event.target.value })} />
                  )}
                  <Button type="button" size="sm" variant="ghost" aria-label={`Remove ${label.toLowerCase()} ${index + 1}`}
                    onClick={() => remove(row)}>
                    Remove
                  </Button>
                </div>
                {(field === 'season' || field === 'location') && (
                  <div className="grid gap-2 sm:grid-cols-3">
                    <label className="space-y-1 text-xs">
                      <span>Rule number</span>
                      <Input aria-label={`${label} rule ${index + 1}`} type="number" min={1} max={256} placeholder="1"
                        value={row.rule ?? ''} onChange={event => update(row, { rule: event.target.value })} />
                    </label>
                    {field === 'location' && (
                      <>
                        <label className="space-y-1 text-xs">
                          <span>Area code scheme</span>
                          <Input aria-label={`Area code scheme ${index + 1}`} maxLength={64} value={row.area?.codeScheme ?? ''}
                            onChange={event => update(row, { area: { ...row.area, codeScheme: event.target.value } })} />
                        </label>
                        <label className="space-y-1 text-xs">
                          <span>Area code</span>
                          <Input aria-label={`Area code ${index + 1}`} maxLength={128} value={row.area?.areaCode ?? ''}
                            onChange={event => update(row, { area: { ...row.area, areaCode: event.target.value } })} />
                        </label>
                      </>
                    )}
                  </div>
                )}
                {row.evidence && <p className="break-words text-xs text-muted-foreground">“{row.evidence.quote}”</p>}
              </div>
            ))}
            {field === 'location' && fieldRows.length > 0 && (
              <p className="text-xs text-muted-foreground">Use the same rule number for a season and location that apply together.</p>
            )}
            <Button type="button" variant="ghost" size="sm" disabled={rows.length >= 256} onClick={() => add(field)}>
              Add {label.toLowerCase()}
            </Button>
            {isChunk && <>
              <Button type="button" variant="ghost" size="sm" onClick={() => clearField(field, false)}>Clear {label.toLowerCase()} for this chunk</Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => clearField(field, true)}>Use document {label.toLowerCase()}</Button>
            </>}
          </fieldset>
        )
      })}
      {!isChunk && review.excluded_areas?.length > 0 && (
        <dl className="space-y-1 text-sm">
          <dt className="font-semibold">Excluded locations</dt>
          <dd className="break-words text-muted-foreground">
            {review.excluded_areas.map(area => area.areaName || area.areaCode).join(', ')}
          </dd>
        </dl>
      )}
      {!isChunk && (
        <fieldset className="space-y-2" disabled={busy}>
          <legend className="px-1 text-sm font-semibold">Document validity</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="space-y-1 text-sm">
              <span>Start date</span>
              <Input type="date" value={validFrom} onChange={event => setValidFrom(event.target.value)} />
            </label>
            <label className="space-y-1 text-sm">
              <span>End date</span>
              <Input type="date" value={validTo} min={validFrom || undefined} onChange={event => setValidTo(event.target.value)} />
            </label>
          </div>
          <p className="text-xs text-muted-foreground">Dates are inclusive. Leave either boundary blank for an open period.</p>
        </fieldset>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>{saving ? 'Saving…' : 'Save metadata'}</Button>
        {onCancel && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={onCancel}>Back to current metadata</Button>}
      </div>
    </form>
  )
}
