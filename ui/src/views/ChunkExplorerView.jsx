import { Fragment, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, Search, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ChunkMetadata from '../components/ChunkMetadata'
import PageNavigation from '../components/PageNavigation'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import {
  buildDocumentChunkUrl,
  fetchJson,
  getStageLabel,
  PIPELINE_STAGES,
} from '../lib/pipelineUi'

const PAGE_SIZE = 50
const PRESET_STORAGE_KEY = 'chunk-explorer-presets-v1'

export default function ChunkExplorerView() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const [chunks, setChunks] = useState([])
  const [total, setTotal] = useState(0)

  const [query, setQuery] = useState('')
  const [stage, setStage] = useState('all')
  const [includeExcluded, setIncludeExcluded] = useState(false)
  const [offset, setOffset] = useState(0)
  const [appliedFilters, setAppliedFilters] = useState({})
  const [expandedChunk, setExpandedChunk] = useState(null)
  const [viewMode, setViewMode] = useState('detailed')
  const [groupMode, setGroupMode] = useState('none')
  const [presetName, setPresetName] = useState('')
  const [presets, setPresets] = useState([])
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const stageOptions = useMemo(() => ['all', ...PIPELINE_STAGES.map(s => s.id), 'failed'], [])

  function openChunkDocument(chunk) {
    if (!chunk?.workflow_id) return
    navigate(buildDocumentChunkUrl(chunk.workflow_id, chunk.chunk_number))
  }

  function openDocumentChunks(workflowId) {
    if (!workflowId) return
    navigate(buildDocumentChunkUrl(workflowId))
  }

  useEffect(() => {
    runSearch({ isInitial: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    try {
      const raw = localStorage.getItem(PRESET_STORAGE_KEY)
      if (!raw) return
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) setPresets(parsed)
    } catch {
      setPresets([])
    }
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(presets))
    } catch {
      // Ignore storage errors.
    }
  }, [presets])

  async function runSearch({ isInitial = false, nextOffset = offset, filters } = {}) {
    try {
      if (isInitial) setLoading(true)
      setSearching(true)
      setError('')
      const active = filters || {
        query,
        stage,
        includeExcluded,
      }
      const params = new URLSearchParams()
      if ((active.query || '').trim()) params.set('q', (active.query || '').trim())
      if (active.stage && active.stage !== 'all') params.set('stage', active.stage)
      if (active.includeExcluded) params.set('include_excluded', 'true')
      params.set('limit', String(PAGE_SIZE))
      params.set('offset', String(nextOffset))

      const response = await fetchJson(`/chunks/search?${params.toString()}`)
      setChunks(Array.isArray(response.items) ? response.items : [])
      setTotal(Number(response.total || 0))
      setOffset(nextOffset)
      setAppliedFilters(active)
      setExpandedChunk(null)
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
      setSearching(false)
    }
  }

  function clearFilters() {
    const resetFilters = {
      query: '',
      stage: 'all',
      includeExcluded: false,
    }
    setQuery(resetFilters.query)
    setStage(resetFilters.stage)
    setIncludeExcluded(resetFilters.includeExcluded)
    runSearch({ nextOffset: 0, filters: resetFilters })
  }

  function buildPresetPayload() {
    return {
      query,
      stage,
      includeExcluded,
      viewMode,
      groupMode,
    }
  }

  function applyPreset(preset) {
    const appliedFilters = {
      query: preset.query || '',
      stage: preset.stage || 'all',
      includeExcluded: Boolean(preset.includeExcluded),
    }
    setQuery(appliedFilters.query)
    setStage(appliedFilters.stage)
    setIncludeExcluded(appliedFilters.includeExcluded)
    setViewMode(preset.viewMode || 'detailed')
    setGroupMode(preset.groupMode || 'none')
    runSearch({ nextOffset: 0, filters: appliedFilters })
  }

  function savePreset() {
    const name = presetName.trim()
    if (!name) return
    const payload = buildPresetPayload()
    setPresets(current => {
      const existingIndex = current.findIndex(item => item.name === name)
      const nextItem = { name, ...payload }
      if (existingIndex >= 0) {
        const next = [...current]
        next[existingIndex] = nextItem
        return next
      }
      return [nextItem, ...current].slice(0, 20)
    })
    setPresetName('')
  }

  function removePreset(name) {
    setPresets(current => current.filter(item => item.name !== name))
  }

  const groupedByDocument = useMemo(() => {
    const groups = new Map()
    chunks.forEach(chunk => {
      const key = chunk.workflow_id || '__unknown__'
      const label = chunk.name || chunk.filename || key
      const existing = groups.get(key) || { key, label, chunks: [] }
      existing.chunks.push(chunk)
      groups.set(key, existing)
    })
    return Array.from(groups.values())
      .map(group => ({
        ...group,
        chunks: group.chunks.sort((a, b) => Number(a.chunk_number || 0) - Number(b.chunk_number || 0)),
      }))
      .sort((a, b) => b.chunks.length - a.chunks.length || a.label.localeCompare(b.label))
  }, [chunks])

  return (
    <div className="page-shell">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Chunk Explorer</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Search chunk content by keyword.
          </p>
        </div>
        <Badge variant="secondary" className="text-xs">
          {loading ? 'Loading chunks…' : `${total.toLocaleString()} matching chunks`}
        </Badge>
      </div>

      <div className="panel p-4 space-y-3">
        <div className="flex flex-col gap-2 xl:flex-row">
          <div className="relative min-w-0 flex-1">
            <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <Input
              aria-label="Chunk keyword"
              value={query}
              onKeyDown={event => {
                if (event.key === 'Enter' && !searching) runSearch({ nextOffset: 0 })
              }}
              onChange={event => setQuery(event.target.value)}
              placeholder="Keyword search (e.g. milking machine)..."
              className="pl-9"
            />
          </div>
          <Select value={stage} onValueChange={setStage}>
            <SelectTrigger aria-label="Stage" className="w-full xl:w-48">
              <SelectValue placeholder="Stage" />
            </SelectTrigger>
            <SelectContent>
              {stageOptions.map(value => (
                <SelectItem key={value} value={value}>
                  {value === 'all' ? 'All stages' : getStageLabel(value)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={() => runSearch({ nextOffset: 0 })} disabled={searching}>
            {searching ? 'Searching…' : 'Search'}
          </Button>
          <Button variant="outline" onClick={clearFilters} disabled={searching}>
            Clear
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <label className="flex items-center gap-2">
            <Checkbox checked={includeExcluded} onCheckedChange={value => setIncludeExcluded(Boolean(value))} />
            Include excluded chunks
          </label>
          <Button
            size="sm"
            variant="ghost"
            className="h-6 text-[10px] ml-auto"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen(current => !current)}
          >
            {advancedOpen ? <ChevronDown className="h-3 w-3 mr-1" /> : <ChevronRight className="h-3 w-3 mr-1" />}
            Advanced filters
          </Button>
        </div>

        {advancedOpen && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium uppercase tracking-wider">Display</span>
            <Button
              size="sm"
              variant={viewMode === 'detailed' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setViewMode('detailed')}
            >
              Detailed
            </Button>
            <Button
              size="sm"
              variant={viewMode === 'compact' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setViewMode('compact')}
            >
              Compact
            </Button>
            <span className="mx-1">·</span>
            <Button
              size="sm"
              variant={groupMode === 'none' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setGroupMode('none')}
            >
              No groups
            </Button>
            <Button
              size="sm"
              variant={groupMode === 'document' ? 'secondary' : 'outline'}
              className="h-6 text-[10px]"
              onClick={() => setGroupMode('document')}
            >
              Group by document
            </Button>
          </div>
        </div>
        )}

        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Saved presets
          </p>
          <div className="flex flex-col md:flex-row gap-2">
            <Input
              value={presetName}
              onChange={event => setPresetName(event.target.value)}
              placeholder="Preset name (e.g. Cattle disease + treatment)"
              className="md:max-w-sm"
            />
            <Button variant="outline" onClick={savePreset}>Save current filters</Button>
          </div>
          {presets.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {presets.map(preset => (
                <Badge key={preset.name} variant="secondary" className="text-[10px] gap-1">
                  <button type="button" onClick={() => applyPreset(preset)} className="hover:underline">
                    {preset.name}
                  </button>
                  <button
                    type="button"
                    onClick={() => removePreset(preset.name)}
                    aria-label={`Delete ${preset.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="panel p-4 border-destructive/30 bg-destructive/5 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="panel page-scroll min-w-0" aria-busy={searching}>
        {loading ? (
          <div className="space-y-3 p-4" role="status" aria-label="Loading chunks">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </div>
        ) : chunks.length === 0 ? (
          <div className="p-8 text-center">
            <p className="text-sm font-medium">{error ? 'Chunks unavailable' : 'No chunks found'}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {error ? 'Run the search again to retry.' : 'Try fewer filters or a broader keyword.'}
            </p>
          </div>
        ) : (
          <table className="w-full table-fixed text-left text-sm" aria-label="Chunks">
            <thead className="sticky top-0 z-10 bg-card text-xs text-muted-foreground">
              <tr className="border-b border-border">
                <th scope="col" className="px-3 py-3 sm:px-4">Document and chunk</th>
                <th scope="col" className="hidden w-24 px-2 py-3 md:table-cell">Pages</th>
                <th scope="col" className="hidden w-24 px-2 py-3 text-right lg:table-cell">Tokens</th>
                <th scope="col" className="hidden w-36 px-4 py-3 md:table-cell">Status</th>
              </tr>
            </thead>
            {(groupMode === 'document' ? groupedByDocument : [{ key: 'results', chunks }]).map(group => (
              <tbody key={group.key}>
                {groupMode === 'document' && (
                  <tr className="border-b border-border bg-muted/60">
                    <th colSpan={4} className="px-4 py-2">
                      <button type="button" className="max-w-full break-words text-left text-sm text-primary hover:underline"
                        onClick={() => openDocumentChunks(group.key)}>{group.label}</button>
                    </th>
                  </tr>
                )}
                {group.chunks.map(chunk => {
                  const key = chunk.id ?? `${chunk.workflow_id}-${chunk.chunk_number}`
                  const open = expandedChunk === key
                  const title = chunk.name || chunk.filename || chunk.workflow_id
                  const fullText = chunk.edited_text ?? chunk.original_text ?? ''
                  const preview = fullText.replace(/^#{1,6}\s+/, '').slice(0, 220)
                  const pages = chunk.page_start === chunk.page_end
                    ? chunk.page_start : `${chunk.page_start}–${chunk.page_end}`
                  const status = chunk.is_excluded ? 'Excluded' : getStageLabel(chunk.stage)
                  return (
                    <Fragment key={key}>
                      <tr className={`border-b border-border align-top ${open ? 'bg-muted/55' : 'hover:bg-muted/35'}`}>
                        <td className="px-3 py-2 sm:px-4">
                          <button type="button" className="flex w-full items-start gap-2 rounded-sm py-1 text-left focus-visible:outline-2 focus-visible:outline-ring"
                            aria-expanded={open} aria-controls={`chunk-detail-${key}`}
                            aria-label={`${title}, chunk ${chunk.chunk_number}`}
                            onClick={() => setExpandedChunk(open ? null : key)}>
                            {open ? <ChevronDown className="mt-0.5 size-4 shrink-0 text-primary" /> : <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted-foreground" />}
                            <span className="min-w-0">
                              <span className="block break-words font-medium">{title}</span>
                              <span className="block text-xs text-muted-foreground">
                                Chunk {chunk.chunk_number}<span className="md:hidden"> · {status}</span>
                              </span>
                              {viewMode === 'detailed' && preview && <span className="mt-1 block truncate text-xs text-muted-foreground">{preview}</span>}
                            </span>
                          </button>
                        </td>
                        <td className="hidden px-2 py-3 text-xs tabular-nums text-muted-foreground md:table-cell">{pages}</td>
                        <td className="hidden px-2 py-3 text-right text-xs tabular-nums text-muted-foreground lg:table-cell">{chunk.token_count ?? 0}</td>
                        <td className="hidden px-4 py-3 md:table-cell">
                          <Badge variant={chunk.is_excluded ? 'destructive' : 'secondary'} className="whitespace-normal">{status}</Badge>
                        </td>
                      </tr>
                      {open && (
                        <tr id={`chunk-detail-${key}`} className="border-b border-border bg-muted/25">
                          <td colSpan={4} className="space-y-3 px-4 py-4 sm:pl-10">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <p className="text-xs text-muted-foreground">
                                Pages {pages} · {chunk.token_count ?? 0} tokens
                                {chunk.is_reviewed && ' · Reviewed'}{chunk.is_reference && ' · Reference'}
                              </p>
                              <Button size="sm" variant="outline" onClick={() => openChunkDocument(chunk)}>Open in document</Button>
                            </div>
                            {chunk.section_title && <p className="break-words font-medium">{chunk.section_title}</p>}
                            <div className="chunk-text max-w-[75ch] break-words leading-relaxed">
                              {fullText ? <ReactMarkdown skipHtml disallowedElements={['img']}>{fullText}</ReactMarkdown> : <p>No text for this chunk.</p>}
                            </div>
                            <ChunkMetadata workflowId={chunk.workflow_id} chunkTags={chunk.tags} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            ))}
          </table>
        )}
      </div>

      <PageNavigation page={Math.floor(offset / PAGE_SIZE) + 1} pageSize={PAGE_SIZE} total={total}
        onPageChange={nextPage => runSearch({ nextOffset: (nextPage - 1) * PAGE_SIZE, filters: appliedFilters })}
        disabled={searching} />
    </div>
  )
}
