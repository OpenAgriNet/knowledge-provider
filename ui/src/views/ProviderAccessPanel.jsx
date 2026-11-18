import { useEffect, useRef, useState } from 'react'
import { UserPlus } from 'lucide-react'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle, SheetTrigger } from '../components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../components/ui/alert-dialog'
import { formatInstanceLabel } from '../lib/instanceLabels'
import { fetchJson } from '../lib/pipelineUi'
import ScheduledSourceStatus from './ScheduledSourceStatus'
import ProviderSourceSettings from './ProviderSourceSettings'
import RegistryProviderPanel from './RegistryProviderPanel'
import WebhookSourcePolicy from './WebhookSourcePolicy'
import { instanceRoles } from './UserAccessForms'

const selectStyle = 'h-9 w-full rounded-md border border-input bg-background px-3 text-sm'

function RoleSelect({ isSuperAdmin, ...props }) {
  return (
    <select className={selectStyle} {...props}>
      {Object.entries(instanceRoles).filter(([value]) => isSuperAdmin || value !== 'state_admin').map(([value, label]) => (
        <option key={value} value={value}>{label}</option>
      ))}
    </select>
  )
}

export default function ProviderAccessPanel({ isSuperAdmin, requestedInstance, revision = 0 }) {
  const [providers, setProviders] = useState([])
  const [provider, setProvider] = useState('')
  const [members, setMembers] = useState([])
  const [memberRevision, setMemberRevision] = useState(0)
  const [sourceRevision, setSourceRevision] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [busy, setBusy] = useState(false)
  const [directoryError, setDirectoryError] = useState('')
  const [memberError, setMemberError] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  const [revoking, setRevoking] = useState(null)
  const [grantOpen, setGrantOpen] = useState(false)
  const [memberQuery, setMemberQuery] = useState('')
  const [memberRole, setMemberRole] = useState('all')
  const appliedInstance = useRef('')
  const path = `/providers/${encodeURIComponent(provider)}/members`
  const query = memberQuery.trim().toLowerCase()
  const visibleMembers = members.filter(member =>
    (memberRole === 'all' || memberRole === member.role) &&
    (!query || `${member.email || ''} ${member.username || ''}`.toLowerCase().includes(query))
  )

  useEffect(() => {
    const controller = new AbortController()
    fetchJson('/providers', { signal: controller.signal }).then(({ providers: options }) => {
      if (controller.signal.aborted) return
      setProviders(options)
      const selectRequested = requestedInstance && appliedInstance.current !== requestedInstance &&
        options.some(option => option.provider === requestedInstance)
      if (selectRequested) appliedInstance.current = requestedInstance
      setProvider(current => selectRequested
        ? requestedInstance
        : options.some(option => option.provider === current) ? current : options[0]?.provider || '')
      setDirectoryError('')
    }).catch(error => {
      if (!controller.signal.aborted) setDirectoryError(error.message)
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [requestedInstance, revision])

  useEffect(() => {
    if (!provider) return
    const controller = new AbortController()
    setMembers([])
    setMemberError('')
    setLoadingMembers(true)
    fetchJson(path, { signal: controller.signal }).then(({ members: result }) => {
      if (!controller.signal.aborted) setMembers(result)
    }).catch(error => {
      if (!controller.signal.aborted) setMemberError(error.message)
    }).finally(() => {
      if (!controller.signal.aborted) setLoadingMembers(false)
    })
    return () => controller.abort()
  }, [path, provider, memberRevision])

  function selectInstance(event) {
    setProvider(event.target.value)
    setMemberQuery('')
    setMemberRole('all')
    setNotice('')
    setActionError('')
  }

  async function change(target, method, data, success) {
    setBusy(true)
    setNotice('')
    setActionError('')
    try {
      await fetchJson(target, {
        method,
        ...(data ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) } : {}),
      })
      setNotice(success)
      setMemberRevision(current => current + 1)
      return true
    } catch (error) {
      setActionError(error.message)
      return false
    } finally {
      setBusy(false)
    }
  }

  async function grant(event) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    if (await change(path, 'POST', { email: fields.get('email').trim(), role: fields.get('role') }, 'Instance access granted.')) {
      setGrantOpen(false)
    }
  }

  async function revoke() {
    if (await change(`${path}/${encodeURIComponent(revoking.user_id)}`, 'DELETE', undefined, 'Instance access revoked.')) {
      setRevoking(null)
    }
  }

  return (
    <section aria-labelledby="instance-access-title" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="instance-access-title" className="text-lg font-semibold">Instance access</h2>
          <p className="mt-1 text-sm text-muted-foreground">Manage members, intake and Registry settings for the selected instance.</p>
        </div>
        {providers.length > 0 && (
          <div className="w-full space-y-1.5 sm:max-w-64">
            <Label htmlFor="managed-instance">Instance</Label>
            <select id="managed-instance" className={selectStyle} value={provider} disabled={busy} onChange={selectInstance}>
              {providers.map(option => <option key={option.provider} value={option.provider}>{formatInstanceLabel(option.provider) ?? 'Default instance'}</option>)}
            </select>
          </div>
        )}
      </div>

      {[directoryError, memberError, revoking || grantOpen ? '' : actionError].filter(Boolean).map((message, index) => (
        <p key={`${index}-${message}`} role="alert" className="text-sm text-destructive">{message}</p>
      ))}
      {notice && <p role="status" className="text-sm text-muted-foreground">{notice}</p>}
      {loading && <p role="status" className="text-sm text-muted-foreground">Loading instances…</p>}
      {!loading && !providers.length && !directoryError && (
        <p className="text-sm text-muted-foreground">{isSuperAdmin ? 'Add an instance to manage access and intake.' : 'You have no instances to manage.'}</p>
      )}

      {provider && (
        <Tabs defaultValue="members" className="space-y-3">
          <TabsList aria-label="Instance settings">
            <TabsTrigger value="members">Members</TabsTrigger>
            <TabsTrigger value="intake">Intake</TabsTrigger>
            <TabsTrigger value="registry">Registry</TabsTrigger>
          </TabsList>

          <TabsContent value="members" className="overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-4 sm:px-5">
              <div className="min-w-35 grow space-y-1.5">
                <Label htmlFor="member-search">Name or email</Label>
                <Input id="member-search" className="h-9" placeholder="Search members" value={memberQuery} onChange={event => setMemberQuery(event.target.value)} />
              </div>
              <div className="grow space-y-1.5 sm:grow-0">
                <Label htmlFor="member-role-filter">Role</Label>
                <select id="member-role-filter" className={selectStyle} value={memberRole} onChange={event => setMemberRole(event.target.value)}>
                  <option value="all">All roles</option>
                  {Object.entries(instanceRoles).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </div>
              <Sheet open={grantOpen} onOpenChange={open => { setGrantOpen(open); setActionError('') }}>
                <SheetTrigger asChild>
                  <Button type="button" disabled={busy}><UserPlus aria-hidden className="size-4" /> Grant access</Button>
                </SheetTrigger>
                <SheetContent className="flex w-full flex-col gap-0 p-0 sm:max-w-md">
                  <SheetHeader className="border-b border-border px-6 py-5 pr-12 text-left">
                    <SheetTitle>Grant instance access</SheetTitle>
                    <SheetDescription>Assign an existing verified account to {formatInstanceLabel(provider) || provider}. The user signs in with their own account.</SheetDescription>
                  </SheetHeader>
                  <form onSubmit={grant} className="flex min-h-0 flex-1 flex-col">
                    <fieldset disabled={busy} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
                      <div className="space-y-1.5">
                        <Label htmlFor="member-email">Member email</Label>
                        <Input id="member-email" name="email" type="email" autoComplete="off" required />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="member-role">Instance role</Label>
                        <RoleSelect id="member-role" name="role" defaultValue="state_view" isSuperAdmin={isSuperAdmin} />
                      </div>
                      {actionError && <p role="alert" className="text-sm text-destructive">{actionError}</p>}
                    </fieldset>
                    <SheetFooter className="border-t border-border px-6 py-4">
                      <Button type="button" variant="outline" disabled={busy} onClick={() => setGrantOpen(false)}>Cancel</Button>
                      <Button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Grant access'}</Button>
                    </SheetFooter>
                  </form>
                </SheetContent>
              </Sheet>
            </div>
            {loadingMembers && <p role="status" className="px-5 py-6 text-sm text-muted-foreground">Loading members…</p>}
            {!loadingMembers && !memberError && !visibleMembers.length && (
              <p className="px-5 py-6 text-sm text-muted-foreground">
                {members.length ? 'No members match these filters.' : 'No users have access to this instance.'}
              </p>
            )}
            <ul className="divide-y divide-border" aria-label="Instance members">
              {visibleMembers.map(member => {
                const label = member.email || member.username || member.user_id
                const canEdit = isSuperAdmin || member.role !== 'state_admin'
                return (
                  <li key={member.user_id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 text-sm sm:px-5">
                    <div className="min-w-0 break-all">{label}{!member.enabled && ' (disabled)'}</div>
                    {canEdit ? (
                      <form className="flex max-w-full flex-wrap items-center gap-2" onSubmit={event => {
                        event.preventDefault()
                        const role = new FormData(event.currentTarget).get('role')
                        change(`${path}/${encodeURIComponent(member.user_id)}`, 'PUT', { role }, 'Instance role updated.')
                      }}>
                        <div className="min-w-35">
                          <RoleSelect key={member.role} name="role" aria-label={`Instance role for ${label}`} defaultValue={member.role} isSuperAdmin={isSuperAdmin} />
                        </div>
                        <Button type="submit" variant="outline" size="sm" disabled={busy}>Save</Button>
                        <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => {
                          setActionError('')
                          setRevoking(member)
                        }}>Revoke</Button>
                      </form>
                    ) : <span className="text-muted-foreground">{instanceRoles[member.role]}</span>}
                  </li>
                )
              })}
            </ul>
          </TabsContent>

          <TabsContent value="intake" className="space-y-4 rounded-xl border border-border bg-card p-4 sm:p-5">
            <WebhookSourcePolicy key={provider} provider={provider} sourceRevision={sourceRevision} />
            <ProviderSourceSettings key={`source-settings-${provider}`} provider={provider} onSaved={() => setSourceRevision(value => value + 1)} />
            <ScheduledSourceStatus key={`scheduled-${provider}-${sourceRevision}`} provider={provider} />
          </TabsContent>

          <TabsContent value="registry" className="rounded-xl border border-border bg-card p-4 sm:p-5">
            <RegistryProviderPanel key={provider} provider={provider} />
          </TabsContent>
        </Tabs>
      )}

      <AlertDialog open={Boolean(revoking)} onOpenChange={open => { if (!open) setRevoking(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Revoke access to {formatInstanceLabel(provider) || provider}?</AlertDialogTitle>
            <AlertDialogDescription>{revoking?.email || revoking?.username || revoking?.user_id} will lose access to this instance.</AlertDialogDescription>
          </AlertDialogHeader>
          {actionError && <p role="alert" className="text-sm text-destructive">{actionError}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction disabled={busy} onClick={event => {
              event.preventDefault()
              revoke()
            }}>Revoke access</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}
