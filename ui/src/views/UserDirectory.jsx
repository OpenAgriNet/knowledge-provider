import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, RefreshCw, Search } from 'lucide-react'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Skeleton } from '../components/ui/skeleton'
import { formatInstanceLabel } from '../lib/instanceLabels'
import { fetchJson } from '../lib/pipelineUi'
import { normalizeProductRole, UserRole } from '../lib/roleCapabilities'
import { instanceRoles } from './UserAccessForms'

const PAGE_SIZE = 10
const RESULT_LIMIT = 200
const ANY_INSTANCE = '__all__'
const selectStyle = 'h-9 w-full rounded-md border border-input bg-background px-3 text-sm sm:w-auto'
const accessLabels = {
  super_admin: 'Super Admin',
  global_viewer: 'Global viewer',
  state: 'Instance roles',
  dashboard: 'No assigned role',
}

function memberships(user) {
  const assignments = user.provider_roles ?? {}
  const codes = new Set([...(user.states ?? []).map(code => code.toLowerCase()), ...Object.keys(assignments)])
  return [...codes].sort().map(code => ({ code, role: assignments[code] }))
}

function AccessSummary({ user }) {
  const assigned = memberships(user)
  return (
    <div className="flex flex-wrap gap-1.5">
      {user.access_type !== 'state' && <Badge variant="secondary">{accessLabels[user.access_type]}</Badge>}
      {assigned.map(({ code, role }) => (
        <Badge key={code} variant="outline">
          {formatInstanceLabel(code) ?? 'Default instance'}{instanceRoles[role] ? ` · ${instanceRoles[role]}` : ''}
        </Badge>
      ))}
    </div>
  )
}

export default function UserDirectory({ revision, onEdit }) {
  const [users, setUsers] = useState([])
  const [query, setQuery] = useState('')
  const [access, setAccess] = useState('all')
  const [instance, setInstance] = useState(ANY_INSTANCE)
  const [status, setStatus] = useState('all')
  const [page, setPage] = useState(1)
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const search = query.trim()
    const params = new URLSearchParams({ max_results: RESULT_LIMIT })
    if (search) params.set('search', search)

    async function load() {
      setLoading(true)
      setError('')
      try {
        const data = await fetchJson(`/admin/users?${params}`, { signal: controller.signal })
        if (controller.signal.aborted) return
        setUsers((data.users ?? []).map(user => {
          const scope = normalizeProductRole(user.access_type) === UserRole.GLOBAL_VIEWER ? 'global_viewer' : user.access_type
          return { ...user, access_type: Object.hasOwn(accessLabels, scope) ? scope : 'dashboard' }
        }))
      } catch (failure) {
        if (!controller.signal.aborted) {
          setUsers([])
          setError(failure.message)
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }

    const timer = window.setTimeout(load, search ? 250 : 0)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [query, reload, revision])

  const assignedInstances = [...new Set(users.flatMap(user => memberships(user).map(({ code }) => code)))].sort()
  const instanceOptions = [...new Set(instance === ANY_INSTANCE ? assignedInstances : [...assignedInstances, instance])]
  const filtered = users.filter(user =>
    (access === 'all' || user.access_type === access) &&
    (status === 'all' || Boolean(user.enabled) === (status === 'active')) &&
    (instance === ANY_INSTANCE || memberships(user).some(({ code }) => code === instance)),
  )
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const currentPage = Math.min(page, Math.max(1, totalPages))
  const start = (currentPage - 1) * PAGE_SIZE
  const activeFilters = Boolean(query) || access !== 'all' || instance !== ANY_INSTANCE || status !== 'all'

  function clearFilters() {
    setQuery('')
    setAccess('all')
    setInstance(ANY_INSTANCE)
    setStatus('all')
    setPage(1)
  }

  return (
    <section aria-label="User directory" className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-4 sm:px-5">
        <div className="min-w-full grow sm:min-w-48">
          <label className="mb-1.5 block text-xs font-medium" htmlFor="directory-search">Name or email</label>
          <div className="relative">
            <Search aria-hidden className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input id="directory-search" className="h-9 pl-9" placeholder="Search users" value={query} onChange={event => { setQuery(event.target.value); setPage(1) }} />
          </div>
        </div>
        <div className="min-w-28 grow sm:grow-0">
          <label className="mb-1.5 block text-xs font-medium" htmlFor="directory-access">Access</label>
          <select id="directory-access" className={selectStyle} value={access} onChange={event => { setAccess(event.target.value); setPage(1) }}>
            <option value="all">All access</option>
            {Object.entries(accessLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        <div className="min-w-28 grow sm:grow-0">
          <label className="mb-1.5 block text-xs font-medium" htmlFor="directory-instance">Instance</label>
          <select id="directory-instance" className={selectStyle} value={instance} onChange={event => { setInstance(event.target.value); setPage(1) }}>
            <option value={ANY_INSTANCE}>All instances</option>
            {instanceOptions.map(code => <option key={code} value={code}>{formatInstanceLabel(code) ?? 'Default instance'}</option>)}
          </select>
        </div>
        <div className="min-w-24 grow sm:grow-0">
          <label className="mb-1.5 block text-xs font-medium" htmlFor="directory-status">Status</label>
          <select id="directory-status" className={selectStyle} value={status} onChange={event => { setStatus(event.target.value); setPage(1) }}>
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="disabled">Disabled</option>
          </select>
        </div>
        <Button type="button" size="icon" variant="outline" aria-label="Refresh user directory" onClick={() => setReload(value => value + 1)} disabled={loading}>
          <RefreshCw aria-hidden className="size-4" />
        </Button>
      </div>

      {error ? (
        <div className="px-5 py-8">
          <p role="alert" className="text-sm text-destructive">Could not load the user directory. {error}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={() => setReload(value => value + 1)}>Try again</Button>
        </div>
      ) : loading ? (
        <div role="status" className="space-y-4 px-5 py-6">
          <span className="sr-only">Loading users</span>
          {Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-8 w-full max-w-lg" />)}
        </div>
      ) : filtered.length === 0 ? (
        <div className="px-5 py-10 text-center">
          <p className="font-medium">{activeFilters ? 'No users match these filters' : 'No users to display'}</p>
          {activeFilters && <Button type="button" size="sm" variant="outline" className="mt-3" onClick={clearFilters}>Clear filters</Button>}
        </div>
      ) : (
        <ul className="divide-y divide-border" aria-label="Users">
          {filtered.slice(start, start + PAGE_SIZE).map(user => (
            <li key={user.user_id} className="grid gap-2 px-4 py-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_4.5rem_auto] sm:items-center sm:gap-4 sm:px-5">
              <div className="min-w-0">
                <p className="break-words text-sm font-medium">{user.name || user.username || user.email}</p>
                {user.email && user.email !== user.name && <p className="break-all text-xs text-muted-foreground">{user.email}</p>}
              </div>
              <AccessSummary user={user} />
              <div className="flex items-center justify-between gap-4 lg:contents">
                <span className={user.enabled ? 'text-sm text-success' : 'text-sm text-muted-foreground'}>{user.enabled ? 'Active' : 'Disabled'}</span>
                <Button type="button" variant="outline" size="sm" aria-label={`Change access for ${user.name || user.email || user.username}`} onClick={event => onEdit(user, event.currentTarget)}>
                  Change access
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {!loading && !error && filtered.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 text-sm text-muted-foreground sm:px-5">
          <span>{start + 1}–{Math.min(start + PAGE_SIZE, filtered.length)} of {filtered.length} users{users.length === RESULT_LIMIT && ' · Refine your search to find more'}</span>
          {totalPages > 1 && (
            <div className="flex items-center gap-2">
              <Button type="button" size="icon" variant="outline" aria-label="Previous page" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}><ChevronLeft aria-hidden className="size-4" /></Button>
              <span>{currentPage} / {totalPages}</span>
              <Button type="button" size="icon" variant="outline" aria-label="Next page" disabled={currentPage >= totalPages} onClick={() => setPage(currentPage + 1)}><ChevronRight aria-hidden className="size-4" /></Button>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
