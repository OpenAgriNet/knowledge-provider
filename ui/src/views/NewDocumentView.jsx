import React, { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import {
  Upload,
  FileText,
  CheckCircle,
  X,
  AlertCircle,
  Clock,
  Loader2,
  ShieldCheck,
  FileUp,
  ArrowRight,
  RefreshCw,
  ScanText,
  Languages,
  Layers,
  Database,
} from 'lucide-react'
import { API_BASE } from '../config'
import UrlIntakeForm from './UrlIntakeForm'
import WebhookSourcePolicy from './WebhookSourcePolicy'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { IntakeMetadataFields, useIntakeMetadata } from './IntakeMetadataFields'
import { apiFetch } from '../auth/keycloak'
import { useAuth } from '../auth/AuthProvider'
import { formatInstanceLabel } from '../lib/instanceLabels'
import { fetchJson, formatCompactDateTime, getDocumentListLabel, summarizeIngestStatus } from '../lib/pipelineUi'
import { displayNameError, filenameStem } from '../lib/displayName'

const SUPPORTED_TYPES = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.csv', '.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.txt', '.md', '.markdown', '.html', '.htm', '.json']
const MAX_SIZE_MB = 100
const MAX_BATCH_FILES = 10
const MAX_UPLOAD_BATCHES = 10
const MAX_UPLOAD_BYTES = MAX_SIZE_MB * 1024 * 1024
const FILE_TYPE_CHIPS = ['PDF', 'Office', 'CSV', 'Images', 'Text', 'HTML', 'JSON']
// Fallback for API versions that do not report document kinds.
const DOCUMENT_TYPE_OPTIONS = [
  { value: 'scheme', label: 'Scheme' },
  { value: 'advisory', label: 'Advisory' },
  { value: 'video', label: 'Video' },
]

const PIPELINE_STEPS = [
  { id: 'ocr', label: 'OCR', icon: ScanText },
  { id: 'translate', label: 'Translate', icon: Languages },
  { id: 'chunk', label: 'Chunk', icon: Layers },
  { id: 'index', label: 'Index', icon: Database },
]

function formatFileSize(bytes) {
  if (!bytes && bytes !== 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function splitUploads(files) {
  const batches = []
  let bytes = 0
  for (const file of files) {
    const last = batches[batches.length - 1]
    if (!last || last.length === MAX_BATCH_FILES || bytes + file.size > MAX_UPLOAD_BYTES) {
      batches.push([file])
      bytes = file.size
    } else {
      last.push(file)
      bytes += file.size
    }
  }
  return batches
}

function statusTone(ingest) {
  if (ingest.status === 'success' || ingest.stage === 'completed') return 'success'
  if (ingest.status === 'failed' || ingest.stage === 'failed') return 'destructive'
  return 'info'
}

export default function NewDocumentView() {
  const navigate = useNavigate()
  const { hasPermission, isSuperAdmin, stateRoles, user } = useAuth()
  const canUpload = hasPermission('upload')
  const [providerOptions, setProviderOptions] = useState(null)
  const [providerError, setProviderError] = useState(false)
  const [providerRefresh, setProviderRefresh] = useState(0)
  const uploadInstances = providerOptions?.providers ?? []
  const [selectedInstance, setSelectedInstance] = useState('')
  const documentInstance = uploadInstances.includes(selectedInstance)
    ? selectedInstance
    : providerOptions?.default_provider || (uploadInstances.length === 1 ? uploadInstances[0] : '')
  const [intakeMethod, setIntakeMethod] = useState('file')
  const canManageWebhook = Boolean(user?.live_provider_access) &&
    (isSuperAdmin || stateRoles?.[documentInstance] === 'state_admin')
  const fileInput = useRef(null)
  const [files, setFiles] = useState([])
  const [batchResults, setBatchResults] = useState(null)
  const isBatch = files.length > 1
  const file = files[0]
  const [displayName, setDisplayName] = useState('')
  const [schemeMatches, setSchemeMatches] = useState([])
  const [documentKind, setDocumentKind] = useState('')
  const [parserOptions, setParserOptions] = useState(null)
  const supportedTypes = parserOptions?.file_extensions ?? SUPPORTED_TYPES
  const documentTypeOptions = parserOptions?.document_kinds?.map((kind) => ({
    value: kind,
    label: kind.split('-').map((word) => word[0].toUpperCase() + word.slice(1)).join(' '),
  })) ?? DOCUMENT_TYPE_OPTIONS
  const [parserProvider, setParserProvider] = useState('default')
  const metadata = useIntakeMetadata(parserOptions?.metadata_profiles, documentKind)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadSuccess, setUploadSuccess] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [validationError, setValidationError] = useState('')
  const [recentIngests, setRecentIngests] = useState([])
  const [loadingRecent, setLoadingRecent] = useState(true)
  const [lastWorkflowId, setLastWorkflowId] = useState('')
  const [pendingStartError, setPendingStartError] = useState('')
  const [retryingStart, setRetryingStart] = useState(false)

  useEffect(() => {
    loadRecent()
  }, [])

  useEffect(() => {
    if (canUpload) fetchJson('/upload/options').then(setParserOptions).catch(() => {})
  }, [canUpload])

  useEffect(() => {
    if (!canUpload) return
    const controller = new AbortController()
    setProviderOptions(null)
    setProviderError(false)
    fetchJson('/upload/providers', { signal: controller.signal })
      .then((response) => { if (!controller.signal.aborted) setProviderOptions(response) })
      .catch(() => { if (!controller.signal.aborted) setProviderError(true) })
    return () => controller.abort()
  }, [canUpload, user?.user_id, providerRefresh])

  useEffect(() => {
    setSchemeMatches([])
    if (!canUpload || isBatch || documentKind !== 'scheme' || !documentInstance || displayName.trim().length < 2) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ instance: documentInstance, name: displayName.trim() })
      fetchJson(`/upload/scheme-matches?${params}`, { signal: controller.signal })
        .then(data => { if (!controller.signal.aborted) setSchemeMatches(data.matches || []) })
        .catch(() => {})
    }, 300)
    return () => { clearTimeout(timer); controller.abort() }
  }, [canUpload, isBatch, documentKind, documentInstance, displayName])

  async function loadRecent() {
    setLoadingRecent(true)
    try {
      const docs = await fetchJson('/documents?limit=10')
      setRecentIngests(Array.isArray(docs) ? docs : [])
    } catch {
      setRecentIngests([])
    } finally {
      setLoadingRecent(false)
    }
  }

  function validateFile(nextFile) {
    const ext = `.${nextFile.name.split('.').pop()?.toLowerCase()}`
    if (!supportedTypes.includes(ext)) {
      return `Unsupported file type: ${ext}. Supported: ${supportedTypes.join(', ')}`
    }
    if (nextFile.size / 1024 / 1024 > MAX_SIZE_MB) {
      return `File too large: ${formatFileSize(nextFile.size)}. Maximum: ${MAX_SIZE_MB} MB`
    }
    return ''
  }

  function handleFiles(selection) {
    const nextFiles = Array.from(selection || [])
    if (!nextFiles.length) return
    const validation = nextFiles.map(validateFile).find(Boolean)
      || (splitUploads(nextFiles).length > MAX_UPLOAD_BATCHES
        && `Choose up to ${MAX_UPLOAD_BATCHES * MAX_BATCH_FILES} files, or fewer for larger files.`)
    setValidationError(validation || '')
    setUploadError('')
    metadata.reset()
    setFiles(validation ? [] : nextFiles)
    setDisplayName(validation ? '' : filenameStem(nextFiles[0].name))
  }

  function handleDrop(event) {
    event.preventDefault()
    setDragging(false)
    handleFiles(event.dataTransfer.files)
  }

  function handleFileChange(event) {
    handleFiles(event.target.files)
  }

  const nameError = file && !isBatch ? displayNameError(displayName) : ''
  async function handleSubmit() {
    if (!file || !documentKind || !documentInstance || !canUpload || nameError || metadata.hasEmptySubject) return
    setUploading(true)
    setUploadError('')
    try {
      function appendMetadata(formData) {
        if (parserProvider !== 'default') formData.append('parser_provider', parserProvider)
        const { knowledge_metadata, valid_from, valid_to } = metadata.values()
        if (knowledge_metadata) formData.append('knowledge_metadata', JSON.stringify(knowledge_metadata))
        if (valid_from) formData.append('valid_from', valid_from)
        if (valid_to) formData.append('valid_to', valid_to)
      }
      if (isBatch) {
        const params = new URLSearchParams({ instance: documentInstance, kind: documentKind })
        const results = []
        for (const batch of splitUploads(files)) {
          const body = new FormData()
          batch.forEach((item) => body.append('files', item))
          appendMetadata(body)
          try {
            const response = await apiFetch(`${API_BASE}/upload/batch?${params}`, { method: 'POST', body })
            const data = await response.json()
            if (!response.ok) throw new Error(data.detail || 'Could not submit these files')
            results.push(...data.items)
          } catch (error) {
            results.push(...batch.map(({ name }) => ({
              filename: name,
              detail: `${error.message}. Check Documents before retrying.`,
            })))
          }
        }
        setBatchResults(results)
        await loadRecent()
        return
      }
      const formData = new FormData()
      formData.append('file', file)
      formData.append('display_name', displayName.trim())
      appendMetadata(formData)
      const params = new URLSearchParams({
        auto_approve: 'false',
        instance: documentInstance,
        kind: documentKind,
      })
      const response = await apiFetch(`${API_BASE}/upload?${params}`, { method: 'POST', body: formData })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'Failed to upload and start workflow')
      setLastWorkflowId(data.workflow_id)
      setPendingStartError(data.available_actions?.includes('retry_upload_start') ? data.error_message : '')
      setUploadSuccess(true)
      await loadRecent()
    } catch (submitError) {
      setUploadError(submitError.message)
    } finally {
      setUploading(false)
    }
  }

  async function retryStart(workflowId) {
    setRetryingStart(true)
    let detail = ''
    try {
      const response = await apiFetch(`${API_BASE}/upload/${encodeURIComponent(workflowId)}/start`, { method: 'POST' })
      if (!response.ok) {
        const body = await response.json()
        detail = body.detail || 'Could not start the document.'
      }
    } catch {
      detail = 'Could not confirm the document started. Try again.'
    }
    if (batchResults) {
      setBatchResults((items) => items.map((item) => item.document?.workflow_id === workflowId
        ? { ...item, status: detail ? 503 : 202, detail }
        : item))
    } else {
      setPendingStartError(detail)
    }
    setRetryingStart(false)
  }

  function resetUpload() {
    setFiles([])
    setDisplayName('')
    setUploadSuccess(false)
    setBatchResults(null)
    setLastWorkflowId('')
    setPendingStartError('')
    setDocumentKind('')
    metadata.reset()
    setUploadError('')
    setValidationError('')
  }

  if (batchResults) {
    return (
      <div className="page-shell">
        <h1 className="font-serif text-xl font-semibold text-foreground">Upload results</h1>
        <ul className="panel divide-y divide-border">
          {batchResults.map((item, index) => (
            <li key={`${item.filename}-${index}`} className="flex flex-wrap items-center justify-between gap-2 p-3">
              <div className="min-w-0">
                <p className="break-words text-sm font-medium">{item.filename}</p>
                <p className={item.status === 202 ? 'text-xs text-muted-foreground' : 'text-xs text-destructive'}>
                  {item.status === 202 ? 'Queued' : item.detail}
                </p>
              </div>
              {item.status === 503 && item.document && (
                <Button variant="outline" size="sm" disabled={retryingStart} onClick={() => retryStart(item.document.workflow_id)}>
                  Retry start
                </Button>
              )}
              {item.document && (
                <Button variant="outline" size="sm" onClick={() => navigate(`/documents/${item.document.workflow_id}`)}>
                  Open document
                </Button>
              )}
            </li>
          ))}
        </ul>
        <Button variant="outline" onClick={resetUpload}>Upload more files</Button>
      </div>
    )
  }

  if (uploadSuccess) {
    return (
      <div className="page-shell">
        <h1 className="font-serif text-xl font-semibold text-foreground">
          {pendingStartError ? 'Document saved' : 'Document queued'}
        </h1>
        <div className="panel overflow-hidden">
          <div className="border-b border-border bg-success/5 px-5 py-8 text-center">
            <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-success/15">
              <CheckCircle className="size-6 text-success" />
            </div>
            <p className="mt-3 text-sm text-muted-foreground">
              {pendingStartError || (
                <><span className="font-medium text-foreground">{file?.name}</span> is running through the pipeline.</>
              )}
            </p>
            <p className="mt-2 font-mono text-[11px] text-muted-foreground break-all">{lastWorkflowId}</p>
          </div>
          <div className="flex flex-col gap-2 p-4 sm:flex-row sm:justify-end">
            <Button variant="outline" size="sm" onClick={resetUpload}>
              Ingest another
            </Button>
            {pendingStartError && (
              <Button variant="outline" size="sm" disabled={retryingStart} onClick={() => retryStart(lastWorkflowId)}>
                Retry start
              </Button>
            )}
            <Button size="sm" onClick={() => navigate(`/documents/${lastWorkflowId}`)}>
              Open document
              <ArrowRight className="ml-1.5 size-3.5" />
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="page-shell">
      {/* Header — compact, full width */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h1 className="font-serif text-xl font-semibold tracking-tight text-foreground">
            Ingest Document
          </h1>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Upload files, submit a URL, or connect a webhook.
          </p>
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <ShieldCheck className="size-3.5 text-primary" />
          Max {MAX_SIZE_MB} MB per file
        </div>
      </div>

      {!canUpload ? (
        <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          <span>You do not have permission to upload documents.</span>
        </div>
      ) : null}

      {/* Full-width grid — no max-width cap */}
      <div className="ingest-layout grid grid-cols-1 items-start gap-6">
        {/* Left column */}
        <div className="min-w-0">
          <Tabs value={intakeMethod} onValueChange={setIntakeMethod} className="grid gap-5">
            <TabsList className="flex h-auto w-fit max-w-full flex-wrap justify-start gap-1" aria-label="Intake method">
              <TabsTrigger value="file">Upload files</TabsTrigger>
              <TabsTrigger value="url">From URL</TabsTrigger>
              <TabsTrigger value="webhook">Webhook</TabsTrigger>
            </TabsList>
          <div className="grid gap-4 rounded-xl border border-border bg-muted/25 p-4 sm:grid-cols-2">
          {intakeMethod !== 'webhook' && <div className="grid min-w-0 content-start gap-2">
            <label htmlFor="document-kind" className="text-xs font-medium text-foreground">
              Document type
            </label>
            <Select value={documentKind} onValueChange={setDocumentKind} disabled={!canUpload}>
              <SelectTrigger id="document-kind" className="h-10 w-full">
                <SelectValue placeholder="Select type…" />
              </SelectTrigger>
              <SelectContent>
                {documentTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>}
          {canUpload && uploadInstances.length > 0 && (
            <div className="grid min-w-0 content-start gap-2">
              <label htmlFor="upload-instance" className="text-xs font-medium text-foreground">Instance</label>
              <Select value={documentInstance} onValueChange={setSelectedInstance} disabled={!canUpload || uploading}>
                <SelectTrigger id="upload-instance" className="h-10 w-full">
                  <SelectValue placeholder="Select an instance…" />
                </SelectTrigger>
                <SelectContent>
                  {uploadInstances.map((instance) => (
                    <SelectItem key={instance} value={instance}>{formatInstanceLabel(instance) ?? 'Default instance'}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {canUpload && providerError && (
            <div className="flex items-center gap-2">
              <p role="alert" className="text-sm text-destructive">Could not load instances.</p>
              <Button type="button" size="sm" variant="outline" onClick={() => setProviderRefresh((count) => count + 1)}>
                Retry loading instances
              </Button>
            </div>
          )}
          {canUpload && !providerError && !providerOptions && (
            <p role="status" className="text-sm text-muted-foreground">Loading instances…</p>
          )}
          {canUpload && providerOptions && uploadInstances.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {isSuperAdmin ? 'Add an instance under Users before starting intake.' : 'Ask an administrator for upload access to an instance.'}
            </p>
          )}
          </div>
          <TabsContent key={intakeMethod} value={intakeMethod} className="space-y-5">
          {intakeMethod === 'webhook' ? (
            documentInstance && (canManageWebhook
              ? <WebhookSourcePolicy key={documentInstance} provider={documentInstance} />
              : <p className="text-sm text-muted-foreground">An instance administrator can set up the webhook and issue its access credentials.</p>)
          ) : intakeMethod === 'url' ? (
            documentInstance && <UrlIntakeForm key={documentInstance} provider={documentInstance} kind={documentKind}
              subjectCategories={parserOptions?.subject_categories} metadataProfiles={parserOptions?.metadata_profiles} onQueued={loadRecent} />
          ) : (<>
          {parserOptions?.parser_providers?.some((provider) => provider !== parserOptions.default_parser) && (
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="upload-parser" className="text-xs font-medium text-foreground">PDF parser</label>
              <Select value={parserProvider} onValueChange={setParserProvider} disabled={uploading}>
                <SelectTrigger id="upload-parser" className="h-8 w-[220px] text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">Deployment default ({parserOptions.default_parser})</SelectItem>
                  {parserOptions.parser_providers.map((provider) => (
                    <SelectItem key={provider} value={provider}>{provider}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          <div
            className={[
              'rounded-xl border-2 border-dashed bg-card transition-colors',
              dragging ? 'border-primary bg-primary/5' : 'border-border',
              validationError ? 'border-destructive/50' : '',
              !dragging && !validationError ? 'hover:border-primary/35' : '',
              !canUpload ? 'opacity-60' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            onDragOver={(event) => {
              event.preventDefault()
              if (canUpload) setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={canUpload ? handleDrop : (event) => event.preventDefault()}
          >
            <div className="px-4 py-6 text-center sm:px-6 sm:py-8">
              {file ? (
                <div className="space-y-3">
                  <div className="mx-auto flex size-11 items-center justify-center rounded-xl bg-primary/10">
                    <FileText className="size-5 text-primary" />
                  </div>
                  <div>
                    <p className="mx-auto max-w-md truncate text-sm font-medium text-foreground">
                      {isBatch ? files.map(({ name }) => name).join(', ') : file.name}
                    </p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {files.length} {isBatch ? 'files' : 'file'} · {formatFileSize(files.reduce((total, item) => total + item.size, 0))} ready
                    </p>
                  </div>
                  <div className="flex items-center justify-center gap-2">
                    <Badge variant="outline" className="text-[10px] font-normal">
                      {`.${file.name.split('.').pop()?.toUpperCase() || 'FILE'}`}
                    </Badge>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => {
                        setFiles([])
                        setDisplayName('')
                        metadata.reset()
                        setValidationError('')
                      }}
                    >
                      <X className="mr-1 size-3.5" />
                      Remove
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="mx-auto flex size-11 items-center justify-center rounded-xl bg-muted">
                    <FileUp
                      className={`size-5 ${dragging ? 'text-primary' : 'text-muted-foreground'}`}
                    />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-foreground">
                      {dragging ? 'Drop file to attach' : 'Drop a file here or browse'}
                    </p>
                    <p className="mt-0.5 text-xs text-muted-foreground">Choose up to 100 files, or fewer for large files.</p>
                  </div>
                  <div className="flex flex-wrap items-center justify-center gap-1">
                    {FILE_TYPE_CHIPS.map((type) => (
                      <span
                        key={type}
                        className="rounded-md border border-border/80 bg-muted/40 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                      >
                        {type}
                      </span>
                    ))}
                  </div>
                  <input
                    ref={fileInput}
                    type="file"
                    multiple
                    className="hidden"
                    disabled={!canUpload}
                    onChange={handleFileChange}
                    accept={supportedTypes.join(',')}
                  />
                  <Button type="button" variant="outline" onClick={() => fileInput.current?.click()} disabled={!canUpload}>
                    <Upload className="mr-1.5 size-3.5" />
                    Browse files
                  </Button>
                </div>
              )}
            </div>
          </div>

          {file && !isBatch ? (
            <div className="space-y-1">
              <label htmlFor="display-name" className="text-sm font-medium text-foreground">
                Display name
              </label>
              <Input
                id="display-name"
                value={displayName}
                aria-invalid={Boolean(nameError)}
                onChange={(event) => setDisplayName(event.target.value)}
              />
              <p className={nameError ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
                {nameError || 'Farmers see this name as the source of their answers.'}
              </p>
            </div>
          ) : null}



          {schemeMatches.length > 0 && (
            <aside className="rounded-lg border border-warning/30 p-3 text-sm" aria-label="Similar schemes">
              <p className="font-medium">A similar scheme already exists. Open the matching record to review it, or continue to create a new document.</p>
              <ul className="mt-1 space-y-1">
                {schemeMatches.map(({ id, name, code }) => (
                  <li key={id}><button type="button" className="underline" onClick={() => navigate(`/documents/${id}`)}>{name} ({code})</button></li>
                ))}
              </ul>
            </aside>
          )}

          {file && <IntakeMetadataFields state={metadata} disabled={!canUpload || uploading}
            subjectCategories={parserOptions?.subject_categories} />}

          {validationError ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              <AlertCircle className="mt-0.5 size-4 shrink-0" />
              <span>{validationError}</span>
            </div>
          ) : null}

          {uploadError ? (
            <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              <AlertCircle className="mt-0.5 size-4 shrink-0" />
              <span className="min-w-0 flex-1">{uploadError}</span>
              <Button variant="ghost" size="sm" className="h-7 shrink-0 text-xs" onClick={handleSubmit}>
                Retry
              </Button>
            </div>
          ) : null}

          {/* Pipeline strip */}
          <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
            {PIPELINE_STEPS.map((step, index) => {
              const Icon = step.icon
              return (
                <React.Fragment key={step.id}>
                  <div className="flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-[11px] text-muted-foreground">
                    <span className="flex size-4 items-center justify-center rounded-full bg-primary/10 text-[9px] font-semibold text-primary">
                      {index + 1}
                    </span>
                    <Icon className="size-3 opacity-70" />
                    {step.label}
                  </div>
                  {index < PIPELINE_STEPS.length - 1 ? (
                    <ArrowRight className="hidden size-3 text-muted-foreground/40 sm:block" />
                  ) : null}
                </React.Fragment>
              )
            })}
          </div>

          <Button
            className="h-10 w-full text-sm font-medium sm:w-auto sm:min-w-[200px]"
            onClick={handleSubmit}
            disabled={!file || !documentKind || !documentInstance || uploading || !canUpload || Boolean(nameError) || metadata.hasEmptySubject}
          >
            {uploading ? (
              <>
                <Loader2 className="mr-2 size-4 animate-spin" />
                Starting…
              </>
            ) : (
              <>
                <Upload className="mr-2 size-4" />
                Start ingestion
              </>
            )}
          </Button>
          </>)}
          </TabsContent>
          </Tabs>
        </div>

        {/* Recent — fills remaining width */}
        <div className="flex min-w-0 flex-col overflow-hidden rounded-xl border border-border bg-card">
          <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
              <Clock className="size-3.5 text-muted-foreground" />
              Recent
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="size-7 p-0"
              onClick={loadRecent}
              disabled={loadingRecent}
              title="Refresh"
            >
              <RefreshCw className={`size-3.5 ${loadingRecent ? 'animate-spin' : ''}`} />
            </Button>
          </div>

          <div className="max-h-[32rem] min-h-0 flex-1 overflow-y-auto">
            {loadingRecent && !recentIngests.length ? (
              <div className="flex items-center justify-center gap-2 px-3 py-10 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                Loading…
              </div>
            ) : recentIngests.length ? (
              <div className="divide-y divide-border">
                {recentIngests.map((ingest) => (
                  <button
                    key={ingest.workflow_id}
                    type="button"
                    className="flex w-full flex-col gap-1 px-3 py-2.5 text-left transition-colors hover:bg-muted/40"
                    onClick={() => navigate(`/documents/${ingest.workflow_id}`)}
                  >
                    <div className="flex items-start gap-2">
                      <FileText className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                        {getDocumentListLabel(ingest)}
                      </span>
                      <Badge variant={statusTone(ingest)} className="shrink-0 text-[10px]">
                        {(ingest.status === 'processing' ||
                          String(ingest.stage || '').includes('processing')) && (
                          <Loader2 className="mr-0.5 size-2.5 animate-spin" />
                        )}
                        {summarizeIngestStatus(ingest)}
                      </Badge>
                    </div>
                    <span className="pl-5 text-[10px] text-muted-foreground">
                      {formatCompactDateTime(ingest.updated_at || ingest.created_at)}
                    </span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="px-3 py-10 text-center">
                <p className="text-xs font-medium text-foreground">No recent uploads</p>
                <p className="mt-0.5 text-[11px] text-muted-foreground">They will show up here.</p>
              </div>
            )}
          </div>

          {recentIngests.length > 0 ? (
            <div className="border-t border-border p-2">
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-full text-xs"
                onClick={() => navigate('/documents')}
              >
                View all documents
                <ArrowRight className="ml-1 size-3" />
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
