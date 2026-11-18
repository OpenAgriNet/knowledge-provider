import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronUp, Code, RotateCcw, Search as SearchIcon, Sliders, AlertCircle } from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import {
  DEFAULT_SEARCH_SETTINGS,
  fetchJson,
  getCandidateRank,
  getCandidateHitId,
  getSearchHighlights,
  getSearchResultSnippet,
  getSearchResultTitle,
  highlightSearchSnippet,
  summarizeCandidateMethod,
} from '../lib/pipelineUi'

// Mirrors the intents the backend already classifies (eligibility, application,
// support), so a first-time query lands on a section the index is tuned for.
const EXAMPLE_QUERIES = [
  'eligibility criteria',
  'how to apply',
  'subsidy amount',
  'documents required',
  'who cannot apply',
]

export default function SearchWorkbenchView() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [schemeChoices, setSchemeChoices] = useState([])
  const [resultQuery, setResultQuery] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [showCandidates, setShowCandidates] = useState(false)
  const [settings, setSettings] = useState(DEFAULT_SEARCH_SETTINGS)
  const [searched, setSearched] = useState(false)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [candidates, setCandidates] = useState([])
  const [collections, setCollections] = useState([])
  const [language, setLanguage] = useState('')
  const [strictLanguage, setStrictLanguage] = useState(false)
  const [areaScheme, setAreaScheme] = useState('')
  const [areaCode, setAreaCode] = useState('')
  const [season, setSeason] = useState('')
  const selectedCollection = collections.find(item => item.name === settings.indexName)
  const networkSearch = Boolean(selectedCollection?.network_id)

  useEffect(() => {
    fetchSettings()
  }, [])

  async function fetchSettings() {
    try {
      const [data, available] = await Promise.all([
        fetchJson('/settings/search'), fetchJson('/search/collections'),
      ])
      setCollections(available)
      setSettings({ ...data, indexName: available.some(({ name }) => name === data.indexName)
        ? data.indexName : available[0]?.name || data.indexName })
    } catch (settingsError) {
      setSearchError(settingsError.message)
    }
  }

  async function handleSearch(overrideQuery, schemeWorkflowId) {
    // Example-query chips pass the text directly: setQuery is async, so reading
    // state here would search the previous value.
    const effectiveQuery = typeof overrideQuery === 'string' ? overrideQuery : query
    if (!effectiveQuery.trim()) return
    if (Boolean(areaScheme.trim()) !== Boolean(areaCode.trim())) {
      setSearchError('Enter both a location code system and a location code.')
      return
    }
    const applicability = {
      ...(areaCode.trim() && { coverage_areas: [{ codeScheme: areaScheme.trim(), areaCode: areaCode.trim() }] }),
      ...(season.trim() && { seasons: [season.trim()] }),
    }
    try {
      setSearching(true)
      setSearchError(null)
      const data = await fetchJson('/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: effectiveQuery,
          ...(schemeWorkflowId && { document_workflow_id: schemeWorkflowId }),
          index_name: settings.indexName,
          ...(networkSearch && {
            network_id: selectedCollection.network_id,
            document_kind: selectedCollection.document_kind,
          }),
          search_mode: settings.searchMethod,
          top_k: settings.limit,
          candidate_cap: settings.candidateCap,
          candidate_multiplier: settings.candidateMultiplier,
          max_chunks_per_doc: settings.maxChunksPerDoc,
          use_e5_prefix: settings.useE5Prefix,
          exclude_reference: settings.excludeReference,
          hybrid_alpha: settings.alpha,
          ranking_method: settings.rankingMethod,
          ef_search: settings.efSearch,
          query_expansion_profile: settings.queryExpansionProfile,
          rerank_mode: settings.rerankMode,
          hybrid_rrf_k: settings.hybridRrfK,
          ...(language.trim() && { language: language.trim(), strict_language: strictLanguage }),
          ...(Object.keys(applicability).length > 0 && { applicability }),
          include_raw_hits: !networkSearch,
        })
      })
      setResults(data.hits || [])
      setSchemeChoices(data.scheme_choices || [])
      setResultQuery(effectiveQuery)
      setCandidates(data.raw_hits || data.candidates || [])
      setSearched(true)
    } catch (err) {
      setSearchError(err.message)
    } finally {
      setSearching(false)
    }
  }

  function handleReset() {
    setSettings({ ...DEFAULT_SEARCH_SETTINGS, indexName: collections[0]?.name || DEFAULT_SEARCH_SETTINGS.indexName })
    setLanguage('')
    setStrictLanguage(false)
    setAreaScheme('')
    setAreaCode('')
    setSeason('')
  }

  function update(key, value) {
    setSettings(current => ({ ...current, [key]: value }))
  }

  const changedSettings = Object.entries(settings).filter(([key, value]) => DEFAULT_SEARCH_SETTINGS[key] !== value)

  return (
    <div className="page-shell">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-serif font-semibold text-foreground">Search Workbench</h1>
          <p className="text-sm text-muted-foreground mt-1">Query the pipeline-managed search index</p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="secondary" className="text-[10px] font-mono">{settings.indexName}</Badge>
          <Badge variant="secondary" className="text-[10px]">{settings.searchMethod}</Badge>
          {settings.searchMethod === 'HYBRID' && (
            <Badge variant="secondary" className="text-[10px]">α={settings.alpha}</Badge>
          )}
          <Badge variant="secondary" className="text-[10px]">top {settings.limit}</Badge>
        </div>
      </div>

      <div className="panel p-4 space-y-3">
        <div className="flex flex-col gap-3 sm:flex-row">
          <div className="relative min-w-0 flex-1">
            <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Enter your search query..."
              aria-label="Search query"
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              className="pl-9 h-11"
            />
          </div>
          <Button onClick={handleSearch} className="h-11 px-6" disabled={searching}>
            {searching ? 'Searching...' : 'Search'}
          </Button>
        </div>

        <div className="grid gap-2 sm:grid-cols-3">
          {collections.length > 0 && (
            <label className="space-y-1 text-xs text-muted-foreground">
              <span>Collection</span>
              <select className="w-full rounded-md border border-input bg-background px-2 py-2 text-sm text-foreground"
                value={settings.indexName} onChange={(event) => update('indexName', event.target.value)}>
                {!collections.some(({ name }) => name === settings.indexName) && <option>{settings.indexName}</option>}
                {collections.map(({ name, label }) => <option value={name} key={name}>{label}</option>)}
              </select>
            </label>
          )}
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Response language</span>
            <Input value={language} placeholder="For example, hi or gu-IN" onChange={(event) => setLanguage(event.target.value)} />
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Location code system</span>
            <Input value={areaScheme} onChange={(event) => setAreaScheme(event.target.value)} />
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Location code</span>
            <Input value={areaCode} onChange={(event) => setAreaCode(event.target.value)} />
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span>Season</span>
            <Input value={season} onChange={(event) => setSeason(event.target.value)} />
          </label>
        </div>
        {language.trim() && <label className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <input type="checkbox" checked={strictLanguage} onChange={(event) => setStrictLanguage(event.target.checked)} />
          Require an exact language match
        </label>}

        <div className="flex flex-wrap items-center justify-between gap-2">
          <button
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
            onClick={() => setShowAdvanced(!showAdvanced)}
          >
            <Sliders className="h-3.5 w-3.5" />
            Advanced settings
            {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
          {changedSettings.length > 0 && !showAdvanced && (
            <span className="text-[10px] text-muted-foreground">
              {changedSettings.length} override{changedSettings.length !== 1 ? 's' : ''} active
            </span>
          )}
        </div>

        {showAdvanced && (
          <div className="space-y-3 pt-2 border-t border-border">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Method</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.searchMethod}
                  onChange={e => update('searchMethod', e.target.value)}
                >
                  <option>HYBRID</option>
                  <option>TENSOR</option>
                  <option>LEXICAL</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Limit</label>
                <Input type="number" value={settings.limit} onChange={e => update('limit', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Alpha</label>
                <Input type="number" step="0.1" min="0" max="1" value={settings.alpha} onChange={e => update('alpha', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Index</label>
                <Input value={settings.indexName} onChange={e => update('indexName', e.target.value)} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">efSearch</label>
                <Input type="number" value={settings.efSearch} onChange={e => update('efSearch', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Candidate Cap</label>
                <Input type="number" value={settings.candidateCap} onChange={e => update('candidateCap', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Max Chunks/Doc</label>
                <Input type="number" value={settings.maxChunksPerDoc} onChange={e => update('maxChunksPerDoc', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Ranking</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.rankingMethod}
                  onChange={e => update('rankingMethod', e.target.value)}
                >
                  <option value="rrf">RRF</option>
                  <option value="normalize_linear">Normalize Linear</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Rerank</label>
                <select
                  className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                  value={settings.rerankMode}
                  onChange={e => update('rerankMode', e.target.value)}
                >
                  <option value="none">None</option>
                  <option value="bm25lite">Keyword ranking</option>
                  <option value="rrf-lite">Rank fusion</option>
                  <option value="heuristic">Text and metadata ranking</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Query Expansion</label>
                <Input
                  className="h-8"
                  value={settings.queryExpansionProfile}
                  onChange={e => update('queryExpansionProfile', e.target.value)}
                  placeholder="none"
                />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Hybrid RRF K</label>
                <Input type="number" value={settings.hybridRrfK} onChange={e => update('hybridRrfK', Number(e.target.value))} className="h-8" />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">Candidate ×</label>
                <Input type="number" value={settings.candidateMultiplier} onChange={e => update('candidateMultiplier', Number(e.target.value))} className="h-8" />
              </div>
            </div>
            <div className="flex items-center gap-4 text-xs">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={settings.showHighlights} onChange={e => update('showHighlights', e.target.checked)} className="rounded" />
                Highlights
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={settings.useE5Prefix} onChange={e => update('useE5Prefix', e.target.checked)} className="rounded" />
                E5 Prefix
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={settings.excludeReference} onChange={e => update('excludeReference', e.target.checked)} className="rounded" />
                Exclude Reference
              </label>
              <Button variant="ghost" size="sm" className="h-6 text-xs ml-auto" onClick={handleReset}>
                <RotateCcw className="h-3 w-3 mr-1" />
                Reset to defaults
              </Button>
            </div>
          </div>
        )}
      </div>

      {searchError && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-destructive/10 border border-destructive/30 text-sm">
          <AlertCircle className="h-4 w-4 text-destructive shrink-0" />
          <span className="text-destructive">{searchError}</span>
        </div>
      )}

      {searching && (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[120px] rounded-lg" />
          ))}
        </div>
      )}

      {!searching && schemeChoices.length > 1 && (
        <div className="panel space-y-2 p-4">
          <p className="text-sm font-medium">Which scheme do you mean?</p>
          <div className="flex flex-wrap gap-2">
            {schemeChoices.map(({ workflow_id, name, code, provider }) => (
              <Button key={workflow_id} type="button" variant="outline" size="sm" className="h-auto max-w-full whitespace-normal break-words text-left"
                onClick={() => handleSearch(resultQuery, workflow_id)}>
                {name} ({code}{provider ? ` · ${provider}` : ''})
              </Button>
            ))}
          </div>
        </div>
      )}

      {searched && !searching && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm text-muted-foreground">
              {results.length} results · {settings.searchMethod} · α={settings.alpha}
            </span>
            {!networkSearch && <button
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
              onClick={() => setShowCandidates(!showCandidates)}
            >
              <Code className="h-3.5 w-3.5" />
              {showCandidates ? 'Hide' : 'Show'} candidate hits
            </button>}
          </div>

          {results.length > 0 ? (
            results.map((result, i) => (
              <div key={i} className="panel p-4 space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="text-lg font-semibold font-serif text-foreground">{Number(result._score || 0).toFixed(2)}</span>
                    {result.workflow_id ? (
                      <Link className="min-w-0 break-words text-sm font-medium text-primary hover:underline"
                        to={`/documents/${encodeURIComponent(result.workflow_id)}${result.revision_id
                          ? `?tab=revisions&revision=${encodeURIComponent(result.revision_id)}` : `?tab=chunks&chunk=${result.chunk_num || ''}`}`}>
                        {getSearchResultTitle(result)}
                      </Link>
                    ) : <span className="text-sm font-medium">{getSearchResultTitle(result)}</span>}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {result.text_language && <span>{result.text_language}{result.language_fallback ? ' · source fallback' : ''}</span>}
                    <span>Chunk {result.chunk_num}</span>
                    <span>·</span>
                    <span>Pages {result.page_start}–{result.page_end}</span>
                  </div>
                </div>
                <p className="break-words text-sm leading-relaxed text-foreground/80">
                  {highlightSearchSnippet(getSearchResultSnippet(result), getSearchHighlights(result)).map((part, index) => (
                    part.highlighted
                      ? <mark key={`${part.text}-${index}`} className="bg-warning/20 text-foreground px-0.5 rounded">{part.text}</mark>
                      : <React.Fragment key={`${part.text}-${index}`}>{part.text}</React.Fragment>
                  ))}
                </p>
                {settings.showHighlights && getSearchHighlights(result).length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {getSearchHighlights(result).map((h, j) => (
                      <Badge key={j} variant="secondary" className="text-xs">{h}</Badge>
                    ))}
                  </div>
                )}
              </div>
            ))
          ) : (
            <div className="panel p-16 text-center">
              <SearchIcon className="h-8 w-8 mx-auto mb-3 text-muted-foreground/30" />
              <p className="text-sm font-medium text-foreground">No results found</p>
              <p className="text-xs text-muted-foreground mt-1">Try adjusting your query or search settings</p>
            </div>
          )}

          {!networkSearch && showCandidates && (
            <div className="panel">
              <div className="panel-header">
                <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Candidate Hits</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[40rem] text-xs">
                  <thead>
                    <tr className="border-b border-border text-left">
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Rank</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Search Score</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Method Score</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Method</th>
                      <th className="px-4 py-2 text-muted-foreground uppercase tracking-wider">Chunk ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidates.map((c, idx) => (
                      <tr key={getCandidateHitId(c) || idx} className="border-b border-border">
                        <td className="px-4 py-2">{getCandidateRank(c, idx)}</td>
                        <td className="px-4 py-2 font-mono">{Number(c._score || c.score || 0).toFixed(3)}</td>
                        <td className="px-4 py-2 font-mono text-muted-foreground">{Number(c.raw_score ?? c._score ?? c.score ?? 0).toFixed(3)}</td>
                        <td className="px-4 py-2">
                          <Badge variant="secondary" className="text-[10px]">{summarizeCandidateMethod(c)}</Badge>
                        </td>
                        <td className="px-4 py-2 font-mono text-muted-foreground">{getCandidateHitId(c)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {!searched && !searching && (
        <div className="panel px-6 py-10">
          <div className="mx-auto flex max-w-xl flex-col items-center text-center">
            <div className="flex size-11 items-center justify-center rounded-xl bg-primary/10">
              <SearchIcon className="size-5 text-primary" />
            </div>
            <p className="mt-3 text-sm font-medium text-foreground">Search the pipeline index</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Search passages and open the source revision to inspect the evidence.
            </p>

            <div className="mt-5 w-full">
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">Try a query</p>
              <div className="mt-2 flex flex-wrap justify-center gap-1.5">
                {EXAMPLE_QUERIES.map(example => (
                  <button
                    key={example}
                    type="button"
                    onClick={() => { setQuery(example); handleSearch(example) }}
                    className="rounded-full border border-border bg-background px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
