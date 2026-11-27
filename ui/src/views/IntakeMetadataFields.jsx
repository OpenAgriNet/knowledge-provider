import { useRef, useState } from 'react'
import { X } from 'lucide-react'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Textarea } from '../components/ui/textarea'
import IntakeProfileFields from './IntakeProfileFields'

const EMPTY_APPLICABILITY = { categories: '', topics: '', tags: '', seasons: '', areaScheme: '', areaCode: '', excludedAreaScheme: '', excludedAreaCode: '', validFrom: '', validTo: '' }

function splitLabels(value) {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}
export function useIntakeMetadata(profiles = [], kind) {
  const [subjects, setSubjects] = useState([])
  const [applicability, setApplicability] = useState(EMPTY_APPLICABILITY)
  const [applicabilityGroups, setApplicabilityGroups] = useState([])
  const nextSubjectId = useRef(0)
  const nextGroupId = useRef(0)
  const [profileValues, setProfileValues] = useState({})
  const [extraAttributes, setExtraAttributes] = useState('')
  const categories = splitLabels(applicability.categories).map(label => label.toLowerCase())
  const availableProfiles = profiles.filter(profile => profile.kind === kind)
  const matches = availableProfiles.filter(profile => categories.includes(profile.subject_category?.toLowerCase()))
  const profile = matches[0] ?? availableProfiles.find(profile => !profile.subject_category)
  const fieldValues = profileValues[profile?.pin.digest] ?? {}

  function updateField(key, value) {
    setProfileValues(current => ({
      ...current,
      [profile.pin.digest]: { ...current[profile.pin.digest], [key]: value },
    }))
  }

  const hasEmptySubject = subjects.some(({ name, code }) => !name.trim() && !code.trim())

  function addSubject() {
    const id = nextSubjectId.current++
    setSubjects((current) => [...current, { id, name: '', code: '', type: '' }])
  }

  function updateSubject(id, field, value) {
    setSubjects((current) => current.map((subject) => subject.id === id ? { ...subject, [field]: value } : subject))
  }

  function values() {
    if (hasEmptySubject) throw new Error('Enter a name or code for each subject.')
    const { categories, topics, tags, seasons, areaScheme, areaCode, excludedAreaScheme, excludedAreaCode, validFrom, validTo } = applicability
    if (Boolean(areaScheme.trim()) !== Boolean(areaCode.trim())) {
      throw new Error('Enter both a location code system and a location code.')
    }
    if (Boolean(excludedAreaScheme.trim()) !== Boolean(excludedAreaCode.trim())) {
      throw new Error('Enter both an excluded location code system and code.')
    }
    const grouped = applicabilityGroups.map(({ areaScheme, areaCode, seasons }) => {
      if (!areaScheme.trim() || !areaCode.trim() || !splitLabels(seasons).length) {
        throw new Error('Enter a code system, location code and at least one season for each location and season rule.')
      }
      return {
        coverage_areas: [{ codeScheme: areaScheme.trim(), areaCode: areaCode.trim() }],
        seasons: splitLabels(seasons),
      }
    })
    if (matches.length > 1) throw new Error('Choose one subject category with a metadata profile.')
    const attributes = extraAttributes.trim() ? JSON.parse(extraAttributes) : {}
    if (!attributes || Array.isArray(attributes) || typeof attributes !== 'object') {
      throw new Error('Additional attributes must be a JSON object with names and values.')
    }
    for (const [key, schema] of Object.entries(profile?.properties ?? {})) {
      const raw = fieldValues[key]
      if (Object.hasOwn(attributes, key)) {
        throw new Error(`Enter ${schema.title || key} in its document attribute field.`)
      }
      if (raw !== undefined && raw !== '') {
        const value = schema.type === 'string' && !schema.enum ? raw : JSON.parse(raw)
        Object.defineProperty(attributes, key, { value, enumerable: true })
      }
    }
    const references = subjects.map(({ name, code, type }) => ({
      ...(type.trim() && { type: type.trim() }),
      descriptor: {
        ...(name.trim() && { name: name.trim() }),
        ...(code.trim() && { code: code.trim() }),
      },
    }))
    const metadata = {
      ...(applicability.broad && { broad_applicability: true }),
      agriculture_subjects: references.filter(ref => !ref.type),
      subjects: references.filter(ref => ref.type),
      applicability_groups: grouped,
      subject_categories: splitLabels(categories),
      ...(Object.keys(attributes).length > 0 && { attributes }),
      ...(profile && { attribute_profile: profile.pin }),
      topics: splitLabels(topics),
      tags: splitLabels(tags),
      seasons: splitLabels(seasons),
      excluded_coverage_areas: excludedAreaCode.trim() ? [{ codeScheme: excludedAreaScheme.trim(), areaCode: excludedAreaCode.trim() }] : [],
      coverage_areas: areaCode.trim() ? [{ codeScheme: areaScheme.trim(), areaCode: areaCode.trim() }] : [],
    }
    return {
      ...(Object.values(metadata).some(value => Array.isArray(value) ? value.length : value) && { knowledge_metadata: metadata }),
      ...(validFrom && { valid_from: validFrom }),
      ...(validTo && { valid_to: validTo }),
    }
  }

  function reset() {
    setSubjects([])
    setApplicability(EMPTY_APPLICABILITY)
    setApplicabilityGroups([])
    setProfileValues({})
    setExtraAttributes('')
  }

  return { subjects, setSubjects, applicability, setApplicability, applicabilityGroups, setApplicabilityGroups,
    nextGroupId, hasEmptySubject, addSubject, updateSubject, values, reset,
    profile, availableProfiles, fieldValues, updateField, extraAttributes, setExtraAttributes }
}

export function IntakeMetadataFields({ state, disabled, subjectCategories = [] }) {
  const { subjects, setSubjects, applicability, setApplicability, applicabilityGroups, setApplicabilityGroups,
    nextGroupId, addSubject, updateSubject, profile, availableProfiles,
    fieldValues, updateField, extraAttributes, setExtraAttributes } = state
  const configuredCategories = [...new Set([
    ...subjectCategories, ...availableProfiles.map(item => item.subject_category).filter(Boolean),
  ])]
  return (
    <details className="rounded-lg border border-border p-3">
      <summary className="cursor-pointer text-sm font-medium text-foreground">{profile?.required.length ? 'Metadata (required fields)' : 'Optional metadata'}</summary>
      <div className="mt-3 space-y-3">
        {configuredCategories.length > 0 && (
        <fieldset className="space-y-2" disabled={disabled}>
            <legend className="text-sm font-medium">Subject categories</legend>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              {configuredCategories.map(category => (
                <label key={category} className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={splitLabels(applicability.categories).includes(category)}
                    onChange={event => {
                      const checked = event.target.checked
                      setApplicability(current => {
                        const labels = splitLabels(current.categories)
                        const next = checked ? [...labels, category] : labels.filter(label => label !== category)
                        return { ...current, categories: next.join(', ') }
                      })
                    }} />
                  {category}
                </label>
              ))}
            </div>
          </fieldset>
        )}
          {profile && <IntakeProfileFields profile={profile} values={fieldValues} onChange={updateField} disabled={disabled} />}
        {profile?.additional_properties !== false && (
          <details className="rounded-md border border-border p-3">
            <summary className="cursor-pointer text-sm font-medium">Additional attributes</summary>
            <label className="mt-3 grid gap-2 text-sm">
              Names and values
              <Textarea value={extraAttributes} disabled={disabled}
                onChange={event => setExtraAttributes(event.target.value)}
                placeholder='{"reference": "A-123", "quantity": 30}' />
            </label>
          </details>
        )}
<fieldset className="space-y-2" disabled={disabled}>
              <legend className="text-sm font-medium text-foreground">Subject details</legend>
              <p className="text-xs text-muted-foreground">
                Enter a subject name or code. You can also add a type, such as crop or livestock.
              </p>
              {subjects.map((subject, index) => (
                <div key={subject.id} className="flex flex-wrap items-end gap-2">
                  {[
                    { field: 'type', label: 'Subject type (optional)', maxLength: 64 },
                    { field: 'name', label: 'Subject name', maxLength: 256 },
                    { field: 'code', label: 'Subject code', maxLength: 256 },
                  ].map(({ field, label, maxLength }) => (
                    <label key={field} className="min-w-32 flex-1 space-y-1 text-xs text-foreground">
                      <span>{label}</span>
                      <Input
                        aria-label={`${label} ${index + 1}`}
                        value={subject[field]}
                        maxLength={maxLength}
                        onChange={(event) => updateSubject(subject.id, field, event.target.value)}
                      />
                    </label>
                  ))}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove subject ${index + 1}`}
                    onClick={() => setSubjects((current) => current.filter(({ id }) => id !== subject.id))}
                  >
                    <X className="size-4" />
                  </Button>
                </div>
              ))}
              <Button type="button" variant="outline" size="sm" onClick={addSubject} disabled={subjects.length >= 32}>
                Add subject
              </Button>
            </fieldset>

<details className="rounded-lg border border-border p-3">
              <summary className="cursor-pointer text-sm font-medium text-foreground">Applicability and tags</summary>
              <fieldset className="mt-3 space-y-3" disabled={disabled}>
                <div className="grid gap-2 sm:grid-cols-2">
                  {[
                    ...(configuredCategories.length ? [] : [['categories', 'Subject categories']]), ['topics', 'Topics'], ['seasons', 'Seasons'], ['tags', 'Tags'],
                    ['areaScheme', 'Included location code system'], ['areaCode', 'Included location code'],
                    ['excludedAreaScheme', 'Excluded location code system'], ['excludedAreaCode', 'Excluded location code'],
                    ['validFrom', 'Valid from', 'date'], ['validTo', 'Valid through', 'date'],
                  ].map(([key, label, type]) => (
                    <label key={key} className="space-y-1 text-xs">
                      <span>{label}</span>
                      <Input type={type || 'text'} value={applicability[key]} onChange={(event) => {
                        const value = event.target.value
                        setApplicability((current) => ({ ...current, [key]: value }))
                      }} />
                    </label>
                  ))}
                </div>
                <label className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={Boolean(applicability.broad)} onChange={(event) => {
                    const broad = event.target.checked
                    setApplicability((current) => ({ ...current, broad }))
                  }} />
                  Confirm broad applicability for any unspecified location or season
                </label>
                {applicabilityGroups.map((group, index) => (
                  <div key={group.id} className="space-y-2 rounded-md border border-border p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-xs font-medium">Location and season rule {index + 1}</p>
                      <Button type="button" size="sm" variant="ghost" onClick={() => {
                        setApplicabilityGroups((current) => current.filter((item) => item.id !== group.id))
                      }}>Remove</Button>
                    </div>
                    <div className="grid gap-2 sm:grid-cols-3">
                      { [['areaScheme', 'Location code system'], ['areaCode', 'Location code'], ['seasons', 'Matching seasons']].map(([key, label]) => (
                        <label key={key} className="space-y-1 text-xs">
                          <span>{label}</span>
                          <Input value={group[key]} onChange={(event) => {
                            const value = event.target.value
                            setApplicabilityGroups((current) => current.map((item) =>
                              item.id === group.id ? { ...item, [key]: value } : item
                            ))
                          }} />
                        </label>
                      ))}
                    </div>
                  </div>
                ))}
                <Button type="button" size="sm" variant="outline" disabled={applicabilityGroups.length >= 32} onClick={() => {
                  const id = nextGroupId.current++
                  setApplicabilityGroups((current) => [...current, { id, areaScheme: '', areaCode: '', seasons: '' }])
                }}>Add location and season rule</Button>
                <p className="text-xs text-muted-foreground">Separate lists with commas. When rules are present, search checks location and season against the same rule. Excluded locations still apply.</p>
              </fieldset>
            </details>

      </div>
    </details>
  )
}
