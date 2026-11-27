import { useEffect, useState } from 'react'
import { Badge } from './ui/badge'
import { fetchJson } from '../lib/pipelineUi'

function Tags({ label, items, empty = 'No tags' }) {
  return (
    <div className="space-y-1.5">
      <dt className="text-xs font-semibold">{label}</dt>
      <dd className="flex flex-wrap gap-1.5">
        {items?.length
          ? items.map(tag => <Badge key={tag} variant="secondary" className="max-w-full whitespace-normal break-all">{tag}</Badge>)
          : <span className="text-xs text-muted-foreground">{empty}</span>}
      </dd>
    </div>
  )
}

export default function ChunkMetadata({ workflowId, chunkTags, documentTags }) {
  const [savedDocument, setSavedDocument] = useState(null)

  useEffect(() => {
    if (documentTags !== undefined || !workflowId) return
    const controller = new AbortController()
    fetchJson(`/documents/${encodeURIComponent(workflowId)}`, { signal: controller.signal })
      .then(doc => {
        if (!controller.signal.aborted) setSavedDocument({ tags: doc.knowledge_metadata?.tags || [] })
      })
      .catch(() => {
        if (!controller.signal.aborted) setSavedDocument({ error: true })
      })
    return () => controller.abort()
  }, [workflowId, documentTags])

  const documentTagStatus = savedDocument?.error ? 'Tags unavailable' : savedDocument || documentTags ? 'No tags' : 'Loading tags…'

  return (
    <div className="min-w-0 space-y-3 border-t border-border pt-3">
      <dl className="grid gap-3 sm:grid-cols-2" aria-label="Saved tags">
        <Tags label="Document tags" items={documentTags ?? savedDocument?.tags} empty={documentTagStatus} />
        <Tags label="Chunk tags" items={chunkTags} />
      </dl>
    </div>
  )
}
