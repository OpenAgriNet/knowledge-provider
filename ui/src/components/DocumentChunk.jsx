import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import ChunkMetadata from './ChunkMetadata'
import MetadataReview from './MetadataReview'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Checkbox } from './ui/checkbox'
import { Input } from './ui/input'
import { Textarea } from './ui/textarea'
import { fetchJson } from '../lib/pipelineUi'

function ChunkEditor({ chunk, workflowId, onSaved }) {
  const savedText = chunk.edited_text ?? chunk.original_text ?? ''
  const savedTags = (chunk.tags || []).join(', ')
  const [text, setText] = useState(savedText)
  const [tags, setTags] = useState(savedTags)
  const [included, setIncluded] = useState(!chunk.is_excluded)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState(null)
  const inclusionChanged = included !== !chunk.is_excluded
  const changed = text !== savedText || tags !== savedTags || inclusionChanged

  async function save(event) {
    event.preventDefault()
    if (!changed || saving) return
    setSaving(true)
    setNotice(null)
    const updates = {}
    if (text !== savedText) updates.edited_text = text
    if (tags !== savedTags) updates.tags = tags.split(',').map(tag => tag.trim()).filter(Boolean)
    if (inclusionChanged) updates.is_excluded = !included
    try {
      const saved = await fetchJson(`/documents/${encodeURIComponent(workflowId)}/chunks/${chunk.chunk_number}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      })
      onSaved(saved)
      setText(saved.edited_text ?? saved.original_text ?? '')
      setTags((saved.tags || []).join(', '))
      setIncluded(!saved.is_excluded)
      setNotice({ text: 'Chunk saved. Reindex to update search.' })
    } catch (error) {
      setNotice({ text: error.message, error: true })
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={save} className="space-y-3 pb-3">
      <label className="block space-y-1.5 text-sm font-medium">
        <span>Chunk text</span>
        <Textarea value={text} onChange={event => setText(event.target.value)} disabled={saving} className="min-h-28 font-mono" />
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        <span>Chunk tags <span className="font-normal text-muted-foreground">(comma separated)</span></span>
        <Input value={tags} onChange={event => setTags(event.target.value)} disabled={saving} />
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={included} onCheckedChange={value => setIncluded(value === true)} disabled={saving} />
          Include in search
        </label>
        <Button type="submit" size="sm" disabled={!changed || saving}>{saving ? 'Saving…' : 'Save changes'}</Button>
      </div>
      {notice && <p role={notice.error ? 'alert' : 'status'} className={notice.error ? 'text-sm text-destructive' : 'text-sm text-muted-foreground'}>{notice.text}</p>}
    </form>
  )
}

export default function DocumentChunk({ chunk, documentTags, workflowId, highlighted, defaultOpen, canEdit, onJump, onSaved, onMetadataSaved }) {
  const [open, setOpen] = useState(defaultOpen)
  const text = chunk.edited_text ?? chunk.original_text ?? ''
  const id = `chunk-card-${chunk.chunk_number}`
  const pages = chunk.page_start === chunk.page_end ? `Page ${chunk.page_start}` : `Pages ${chunk.page_start}–${chunk.page_end}`

  return (
    <section id={id} className={`panel scroll-mt-4 ${highlighted ? 'ring-2 ring-primary/70' : ''}`}>
      <button type="button" aria-expanded={open} aria-controls={`${id}-body`}
        onClick={() => setOpen(current => !current)}
        className="flex w-full min-w-0 items-start gap-2 p-3 text-left hover:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:p-4">
        <ChevronRight aria-hidden="true" className={`mt-0.5 size-4 shrink-0 text-muted-foreground ${open ? 'rotate-90' : ''}`} />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            <span className="font-semibold">Chunk {chunk.chunk_number}</span>
            <span className="text-xs text-muted-foreground">{pages}</span>
            {chunk.is_reviewed && <Badge variant="success">Reviewed</Badge>}
            {chunk.is_excluded && <Badge variant="destructive">Excluded</Badge>}
          </span>
          {!open && <span className="mt-1 block truncate text-xs text-muted-foreground">{text.replace(/^#{1,6}\s+/, '') || 'No text for this chunk'}</span>}
        </span>
      </button>
      <div id={`${id}-body`} hidden={!open} className="min-w-0 space-y-4 border-t border-border p-3 sm:p-4">
        {open && (
          <>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="chunk-text min-w-0 max-w-[75ch] flex-1 break-words text-sm leading-relaxed">
                {text ? <ReactMarkdown skipHtml disallowedElements={['img']}>{text}</ReactMarkdown> : <p>No text for this chunk.</p>}
              </div>
              {onJump && <Button variant="outline" size="sm" onClick={onJump}>Jump to source</Button>}
            </div>
            <MetadataReview workflowId={workflowId} chunkNumber={chunk.chunk_number} canEdit={canEdit} onSaved={onMetadataSaved} />
            <ChunkMetadata workflowId={workflowId} chunkTags={chunk.tags} documentTags={documentTags} />
            {canEdit && (
              <details className="rounded-lg border border-border px-3">
                <summary className="cursor-pointer py-2 text-sm font-medium">Edit chunk</summary>
                <ChunkEditor chunk={chunk} workflowId={workflowId} onSaved={onSaved} />
              </details>
            )}
          </>
        )}
      </div>
    </section>
  )
}
