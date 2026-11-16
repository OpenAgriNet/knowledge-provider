import { useId, useState } from 'react'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from '../components/ui/sheet'
import { fetchJson } from '../lib/pipelineUi'

export const instanceRoles = {
  state_admin: 'Instance administrator',
  state_approver: 'Approver',
  state_contributor: 'Contributor',
  state_view: 'Read only',
}

const selectStyle = 'h-10 w-full rounded-md border border-input bg-background px-3 text-sm'
const limitedAccess = { access_type: 'state', state: '', role: 'state_view' }

function AccessFields({ value, onChange, instances }) {
  const id = useId()
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-scope`}>Access</Label>
        <select id={`${id}-scope`} className={selectStyle} value={value.access_type} onChange={event => onChange('access_type', event.target.value)}>
          <option value="state">Instance role</option>
          <option value="global_viewer">Global viewer</option>
          <option value="super_admin">Super Admin</option>
        </select>
      </div>
      {value.access_type === 'state' && (
        <>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-instance`}>Instance</Label>
            <select id={`${id}-instance`} className={selectStyle} value={value.state} required onChange={event => onChange('state', event.target.value)}>
              <option value="">Select an instance</option>
              {instances.map(instance => <option key={instance.code} value={instance.code}>{instance.label || instance.code}</option>)}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-role`}>Role</Label>
            <select id={`${id}-role`} className={selectStyle} value={value.role} onChange={event => onChange('role', event.target.value)}>
              {Object.entries(instanceRoles).map(([role, label]) => <option key={role} value={role}>{label}</option>)}
            </select>
          </div>
          {instances.length === 0 && <p className="text-sm text-muted-foreground">Add an instance before assigning an instance role.</p>}
        </>
      )}
      {value.access_type === 'super_admin' && <p className="text-sm text-muted-foreground">Manage users and settings across all instances.</p>}
      {value.access_type === 'global_viewer' && <p className="text-sm text-muted-foreground">Browse and search documents across all instances.</p>}
    </div>
  )
}

function initialAccess(user) {
  if (!user) return limitedAccess
  if (user.access_type === 'super_admin' || user.access_type === 'global_viewer') {
    return { ...limitedAccess, access_type: user.access_type }
  }
  const instance = user.states?.[0]
  const role = instance && user.provider_roles?.[instance.toLowerCase()]
  return { ...limitedAccess, state: instance || '', role: instanceRoles[role] ? role : limitedAccess.role }
}

function UserAccessForm({ user, instances, onCancel, onSaved }) {
  const id = useId()
  const [access, setAccess] = useState(() => initialAccess(user))
  const [enabled, setEnabled] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const editing = Boolean(user)

  async function submit(event) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    const assignment = {
      access_type: access.access_type,
      state: access.access_type === 'state' ? access.state : '',
      role: access.access_type === 'state' ? access.role : '',
    }
    const payload = editing ? assignment : {
      ...assignment,
      email: fields.get('email').trim(),
      first_name: fields.get('first_name').trim(),
      last_name: fields.get('last_name').trim(),
      enabled,
    }
    try {
      const result = await fetchJson(editing ? `/admin/users/${encodeURIComponent(user.user_id)}/access` : '/admin/users', {
        method: editing ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      onSaved(result)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
      <fieldset disabled={busy} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
        {!editing && (
          <>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-email`}>Email address</Label>
              <Input id={`${id}-email`} type="email" name="email" autoComplete="off" required />
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor={`${id}-first`}>First name</Label>
                <Input id={`${id}-first`} type="text" name="first_name" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${id}-last`}>Last name</Label>
                <Input id={`${id}-last`} type="text" name="last_name" />
              </div>
            </div>
          </>
        )}
        <AccessFields value={access} onChange={(key, value) => setAccess(current => ({ ...current, [key]: value }))} instances={instances} />
        {editing ? (
          <p className="text-sm text-muted-foreground">This changes access in the selected scope. The user may need to sign in again to see the update.</p>
        ) : (
          <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
            <Label htmlFor={`${id}-active`}>Active account</Label>
            <input id={`${id}-active`} type="checkbox" className="size-4 accent-primary" checked={enabled} onChange={event => setEnabled(event.target.checked)} />
          </div>
        )}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      </fieldset>
      <SheetFooter className="border-t border-border px-6 py-4">
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy || (access.access_type === 'state' && !access.state)}>
          {busy ? 'Saving…' : editing ? 'Save role' : 'Add user'}
        </Button>
      </SheetFooter>
    </form>
  )
}

export function UserAccessFormSheet({ open, user, instances, onClose, onSaved, restoreFocus }) {
  return (
    <Sheet open={open} onOpenChange={value => !value && onClose()}>
      <SheetContent className="flex w-full flex-col gap-0 p-0 sm:max-w-md" onCloseAutoFocus={restoreFocus}>
        <SheetHeader className="border-b border-border px-6 py-5 pr-12 text-left">
          <SheetTitle>{user ? 'Change access' : 'Add user'}</SheetTitle>
          <SheetDescription>{user
            ? (user.email || user.username)
            : 'Add an account by email. The user signs in with their own account.'}</SheetDescription>
        </SheetHeader>
        {open && <UserAccessForm key={user?.user_id ?? 'new'} user={user} instances={instances} onCancel={onClose} onSaved={onSaved} />}
      </SheetContent>
    </Sheet>
  )
}

export function InstanceFormSheet({ open, onClose, onSaved, restoreFocus }) {
  const id = useId()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    const code = fields.get('code').trim().toLowerCase()
    setBusy(true)
    setError('')
    try {
      await fetchJson(`/providers/${encodeURIComponent(code)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ admin_email: fields.get('email').trim() }),
      })
      onSaved(code)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setBusy(false)
    }
  }

  function close() {
    setError('')
    onClose()
  }

  return (
    <Sheet open={open} onOpenChange={value => !value && close()}>
      <SheetContent className="flex w-full flex-col gap-0 p-0 sm:max-w-md" onCloseAutoFocus={restoreFocus}>
        <SheetHeader className="border-b border-border px-6 py-5 pr-12 text-left">
          <SheetTitle>Add instance</SheetTitle>
          <SheetDescription>Choose a code and appoint an administrator whose account already exists.</SheetDescription>
        </SheetHeader>
        <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
          <fieldset disabled={busy} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-code`}>Instance code</Label>
              <Input id={`${id}-code`} name="code" placeholder="rice-lab" maxLength={63} pattern={'[A-Za-z0-9](?:[A-Za-z0-9\\-]{0,61}[A-Za-z0-9])?'} required />
              <p className="text-sm text-muted-foreground">Use letters, numbers and hyphens. This code is part of intake URLs.</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-email`}>Administrator email</Label>
              <Input id={`${id}-email`} type="email" name="email" autoComplete="off" required />
              <p className="text-sm text-muted-foreground">Use a verified email. Add the administrator first if they do not have an account.</p>
            </div>
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          </fieldset>
          <SheetFooter className="border-t border-border px-6 py-4">
            <Button type="button" variant="outline" onClick={close}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? 'Creating…' : 'Create instance'}</Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  )
}
