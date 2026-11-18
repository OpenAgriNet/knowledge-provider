import { useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from './ui/button'
import { Input } from './ui/input'

export default function PageNavigation({ page, pageSize, total, onPageChange, disabled = false }) {
  const [draft, setDraft] = useState(null)
  const lastPage = Math.max(1, Math.ceil(total / pageSize))

  if (lastPage <= 1) return null

  return (
    <nav aria-label="Pagination" className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-border px-4 py-3">
      <p className="text-xs text-muted-foreground tabular-nums" role="status">
        Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} of {total.toLocaleString()}
      </p>
      <form className="flex flex-wrap items-center gap-2" onSubmit={event => {
        event.preventDefault()
        const target = Number(draft ?? page)
        if (!disabled && Number.isInteger(target) && target >= 1 && target <= lastPage) {
          onPageChange(target)
          setDraft(null)
        }
      }}>
        <Button type="button" variant="outline" size="icon" className="size-8" aria-label="Previous page"
          disabled={disabled || page <= 1} onClick={() => { setDraft(null); onPageChange(page - 1) }}>
          <ChevronLeft className="size-4" />
        </Button>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          Page
          <Input type="number" inputMode="numeric" min={1} max={lastPage} required disabled={disabled}
            className="h-8 w-[5.25rem] tabular-nums" value={draft ?? page}
            onChange={event => setDraft(event.target.value)} />
        </label>
        <span className="text-xs tabular-nums text-muted-foreground">of {lastPage.toLocaleString()}</span>
        {draft !== null && <Button type="submit" variant="outline" size="sm" disabled={disabled}>Go</Button>}
        <Button type="button" variant="outline" size="icon" className="size-8" aria-label="Next page"
          disabled={disabled || page >= lastPage} onClick={() => { setDraft(null); onPageChange(page + 1) }}>
          <ChevronRight className="size-4" />
        </Button>
      </form>
    </nav>
  )
}
