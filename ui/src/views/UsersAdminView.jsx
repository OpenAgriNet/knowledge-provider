import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Building2, UserPlus } from 'lucide-react'
import { useAuth } from '../auth/AuthProvider'
import { appPath } from '../basePath'
import { Button } from '../components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { formatInstanceLabel } from '../lib/instanceLabels'
import { fetchJson } from '../lib/pipelineUi'
import ProviderAccessPanel from './ProviderAccessPanel'
import UserDirectory from './UserDirectory'
import { InstanceFormSheet, UserAccessFormSheet, instanceRoles } from './UserAccessForms'

function OwnAccess({ user, isSuperAdmin, isGlobalViewer, instances, stateRoles }) {
  const assignments = [...new Set([...(instances ?? []).map(code => code.toLowerCase()), ...Object.keys(stateRoles ?? {})])].sort()
  const allInstances = isSuperAdmin || isGlobalViewer
  return (
    <div className="page-shell">
      <div>
        <h1 className="font-serif text-2xl font-semibold tracking-tight">Your access</h1>
        <p className="mt-1 text-sm text-muted-foreground">Signed in as {user?.email || user?.name || user?.username || 'your own account'}.</p>
      </div>
      <section aria-labelledby="your-instances" className="max-w-2xl overflow-hidden rounded-xl border border-border bg-card">
        <div className="border-b border-border px-5 py-4">
          <h2 id="your-instances" className="font-semibold">Instances</h2>
          {allInstances && <p className="mt-1 text-sm text-muted-foreground">{isSuperAdmin ? 'You have Super Admin access across all instances.' : 'You can browse and search across all instances as a global viewer.'}</p>}
        </div>
        {assignments.length ? (
          <ul className="divide-y divide-border" aria-label="Your instance assignments">
            {assignments.map(code => (
              <li key={code} className="flex flex-wrap items-baseline justify-between gap-2 px-5 py-3 text-sm">
                <span className="font-medium">{formatInstanceLabel(code) ?? 'Default instance'}</span>
                <span className="text-muted-foreground">{instanceRoles[stateRoles?.[code]] || 'Member'}</span>
              </li>
            ))}
          </ul>
        ) : !allInstances ? <p className="px-5 py-4 text-sm text-muted-foreground">Contact your administrator to request instance access.</p> : null}
      </section>
      <Button asChild variant="outline"><Link to="/documents">Browse documents</Link></Button>
    </div>
  )
}

function ManagedAccess({ canManageUsers, canManageInstances, isSuperAdmin }) {
  const [params, setParams] = useSearchParams()
  const [form, setForm] = useState(null)
  const [options, setOptions] = useState([])
  const [optionsError, setOptionsError] = useState('')
  const [revision, setRevision] = useState(0)
  const [selectedInstance, setSelectedInstance] = useState('')
  const [notice, setNotice] = useState(null)
  const [copied, setCopied] = useState(false)
  const trigger = useRef(null)
  const showBoth = canManageUsers && canManageInstances
  const view = (canManageInstances && (!canManageUsers || params.get('view') === 'instances')) ? 'instances' : 'directory'

  useEffect(() => {
    if (!canManageUsers) return
    const controller = new AbortController()
    fetchJson('/admin/access-options', { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return
      setOptions(result.states ?? [])
      setOptionsError(result.keycloak_admin_configured === false ? 'Configure Keycloak management on the API to update user accounts.' : '')
    }).catch(failure => {
      if (!controller.signal.aborted) setOptionsError(failure.message)
    })
    return () => controller.abort()
  }, [canManageUsers, revision])

  function visit(next) {
    setParams(next === 'instances' ? { view: 'instances' } : {})
  }

  function open(action, target, user = null) {
    trigger.current = target
    setForm({ action, user })
    setNotice(null)
    setCopied(false)
  }

  function restoreFocus(event) {
    if (trigger.current?.isConnected) {
      event.preventDefault()
      trigger.current.focus()
    }
  }

  function userSaved(result) {
    const created = form?.action === 'user' && result.created
    setNotice({ message: created ? 'User added.' : 'Access updated.', email: created ? result.email : '' })
    setForm(null)
    setRevision(value => value + 1)
  }

  function instanceSaved(instance) {
    setSelectedInstance(instance)
    setNotice({ message: `Instance ${formatInstanceLabel(instance) || instance} saved.` })
    setForm(null)
    setRevision(value => value + 1)
    visit('instances')
  }

  async function copySignIn() {
    const signInUrl = new URL(appPath('/login'), window.location.origin).href
    const message = `Open ${signInUrl} and sign in with your own account using ${notice.email}.`
    try {
      await navigator.clipboard.writeText(message)
      setCopied(true)
    } catch {
      setNotice(current => ({ ...current, copyFailed: true }))
    }
  }

  return (
    <div className="page-shell">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-2xl font-semibold tracking-tight">Users</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {canManageUsers ? 'Manage sign-in accounts and access across all instances.' : 'Manage membership in instances you administer.'}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canManageInstances && isSuperAdmin && (
            <Button type="button" variant="outline" onClick={event => open('instance', event.currentTarget)}>
              <Building2 aria-hidden className="size-4" /> Add instance
            </Button>
          )}
          {canManageUsers && (
            <Button type="button" onClick={event => open('user', event.currentTarget)}>
              <UserPlus aria-hidden className="size-4" /> Add user
            </Button>
          )}
        </div>
      </div>

      {notice && (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-muted/40 px-4 py-3 text-sm">
          <p>{notice.message}{notice.email && ` ${notice.email} signs in with their own account.`}</p>
          {notice.email && <Button type="button" variant="outline" size="sm" onClick={copySignIn}>{copied ? 'Copied' : 'Copy sign-in instructions'}</Button>}
          {notice.copyFailed && <p className="text-muted-foreground">Could not copy. Share the console sign-in page directly.</p>}
        </div>
      )}
      {optionsError && <p role="alert" className="text-sm text-destructive">{optionsError}</p>}

      {showBoth ? (
        <Tabs value={view} onValueChange={visit}>
          <TabsList aria-label="User administration">
            <TabsTrigger value="directory">User directory</TabsTrigger>
            <TabsTrigger value="instances">Instances</TabsTrigger>
          </TabsList>
          <TabsContent value="directory" className="mt-4">
            <UserDirectory revision={revision} instances={options} onEdit={(user, target) => open('access', target, user)} />
          </TabsContent>
          <TabsContent value="instances" className="mt-4">
            <ProviderAccessPanel isSuperAdmin={isSuperAdmin} requestedInstance={selectedInstance} revision={revision} />
          </TabsContent>
        </Tabs>
      ) : canManageInstances ? (
        <ProviderAccessPanel isSuperAdmin={isSuperAdmin} requestedInstance={selectedInstance} revision={revision} />
      ) : (
        <UserDirectory revision={revision} instances={options} onEdit={(user, target) => open('access', target, user)} />
      )}

      {canManageUsers && (
        <UserAccessFormSheet
          open={form?.action === 'user' || form?.action === 'access'}
          user={form?.user}
          instances={options}
          onClose={() => setForm(null)}
          onSaved={userSaved}
          restoreFocus={restoreFocus}
        />
      )}
      {canManageInstances && isSuperAdmin && (
        <InstanceFormSheet
          open={form?.action === 'instance'}
          onClose={() => setForm(null)}
          onSaved={instanceSaved}
          restoreFocus={restoreFocus}
        />
      )}
    </div>
  )
}

export default function UsersAdminView() {
  const [params] = useSearchParams()
  const { hasPermission, isSuperAdmin, isGlobalViewer, user, stateRoles, instances } = useAuth()
  const canManageUsers = isSuperAdmin || hasPermission('manage_users')
  const canManageInstances = Boolean(user?.live_provider_access) && (
    isSuperAdmin || Object.values(stateRoles ?? {}).includes('state_admin')
  )

  if (params.get('view') === 'account' || (!canManageUsers && !canManageInstances)) {
    return <OwnAccess user={user} isSuperAdmin={isSuperAdmin} isGlobalViewer={isGlobalViewer} instances={instances} stateRoles={stateRoles} />
  }

  return <ManagedAccess canManageUsers={canManageUsers} canManageInstances={canManageInstances} isSuperAdmin={isSuperAdmin} />
}
