import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth } from '../auth/AuthProvider'
import { UserAccessSheet } from '../components/UserAccessSheet'
import { fetchJson } from '../lib/pipelineUi'
import UsersAdminView from './UsersAdminView'

vi.mock('../auth/AuthProvider', () => ({ useAuth: vi.fn() }))
vi.mock('../lib/pipelineUi', async importOriginal => ({ ...await importOriginal(), fetchJson: vi.fn() }))

const accounts = [
  { user_id: 'one', name: 'Alice Rao', email: 'alice@example.test', enabled: true, access_type: 'state', states: ['RICE-LAB'], provider_roles: { 'rice-lab': 'state_view' } },
  { user_id: 'two', name: 'Bryan Hall', email: 'bryan@example.test', enabled: false, access_type: 'global_viewer', states: [] },
]

function renderPage(auth, path = '/users') {
  useAuth.mockReturnValue({
    hasPermission: () => false, isSuperAdmin: false, isGlobalViewer: false, user: {}, stateRoles: {}, instances: [], ...auth,
  })
  return render(<MemoryRouter initialEntries={[path]}><UsersAdminView /></MemoryRouter>)
}

beforeEach(() => {
  fetchJson.mockReset()
})

describe('Users', () => {
  it('filters the platform directory by instance, access and status', async () => {
    fetchJson.mockImplementation(async path => {
      if (path === '/admin/access-options') return { states: [{ code: 'RICE-LAB' }, { code: 'EMPTY-LAB' }] }
      if (path === '/admin/users?max_results=200') return { users: accounts }
      throw new Error(`Unexpected request: ${path}`)
    })
    renderPage({ isSuperAdmin: true, user: { live_provider_access: true } })

    const user = userEvent.setup()
    expect(await screen.findByText('Alice Rao')).toBeVisible()
    expect(screen.getByText('Bryan Hall')).toBeVisible()
    expect(screen.getByRole('option', { name: 'EMPTY-LAB' })).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Status'), 'disabled')
    expect(screen.queryByText('Alice Rao')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Access'), 'state')
    expect(screen.getByText('No users match these filters')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await user.selectOptions(screen.getByLabelText('Instance'), 'rice-lab')
    expect(screen.getByText('Alice Rao')).toBeVisible()
    expect(screen.queryByText('Bryan Hall')).not.toBeInTheDocument()
  })

  it('opens different sheets for creating a user and creating an instance', async () => {
    let created = false
    fetchJson.mockImplementation(async (path, options = {}) => {
      if (path === '/admin/access-options') return { states: [{ code: 'RICE-LAB' }] }
      if (path === '/admin/users?max_results=200') return { users: accounts }
      if (path === '/admin/users' && options.method === 'POST') return { created: true, email: 'new@example.test' }
      if (path === '/providers/rice-lab' && options.method === 'PUT') { created = true; return {} }
      if (path === '/providers') return { providers: created ? [{ id: 'rice-id', provider: 'rice-lab' }] : [] }
      if (path === '/providers/rice-lab/members') return { members: [] }
      throw new Error(`Unexpected request: ${path}`)
    })
    renderPage({ isSuperAdmin: true, user: { live_provider_access: true } })

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Add user' }))
    const addUser = await screen.findByRole('dialog', { name: 'Add user' })
    expect(addUser).toHaveTextContent('signs in with their own account')
    expect(within(addUser).queryByLabelText(/password/i)).not.toBeInTheDocument()
    await user.type(within(addUser).getByLabelText('Email address'), 'new@example.test')
    await user.selectOptions(within(addUser).getByLabelText('Instance'), 'RICE-LAB')
    await user.click(within(addUser).getByRole('button', { name: 'Add user' }))
    expect(await screen.findByText(/User added.*new@example.test/)).toBeVisible()
    const [, addRequest] = fetchJson.mock.calls.find(([path, options]) => path === '/admin/users' && options.method === 'POST')
    expect(JSON.parse(addRequest.body)).toMatchObject({ email: 'new@example.test', state: 'RICE-LAB', role: 'state_view' })

    await user.click(screen.getByRole('button', { name: 'Add instance' }))
    const addInstance = await screen.findByRole('dialog', { name: 'Add instance' })
    await user.type(within(addInstance).getByLabelText('Instance code'), 'Rice-Lab')
    await user.type(within(addInstance).getByLabelText('Administrator email'), 'owner@example.test')
    await user.click(within(addInstance).getByRole('button', { name: 'Create instance' }))
    expect(await screen.findByText('Instance RICE-LAB saved.')).toBeVisible()
    expect(await screen.findByLabelText('Instance')).toHaveValue('rice-lab')
    const [, instanceRequest] = fetchJson.mock.calls.find(([path]) => path === '/providers/rice-lab')
    expect(JSON.parse(instanceRequest.body)).toEqual({ admin_email: 'owner@example.test' })
  })

  it('limits instance administrators to memberships without loading platform accounts', async () => {
    fetchJson.mockImplementation(async path => {
      if (path === '/providers') return { providers: [{ id: 'rice-id', provider: 'rice-lab' }] }
      if (path === '/providers/rice-lab/members') return { members: [
        { user_id: 'member', email: 'member@example.test', role: 'state_view', enabled: true },
      ] }
      throw new Error(`Unexpected request: ${path}`)
    })
    renderPage({ user: { live_provider_access: true }, stateRoles: { 'rice-lab': 'state_admin' } })

    expect(await screen.findByText('member@example.test')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Grant access' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add user' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add instance' })).not.toBeInTheDocument()
    expect(fetchJson.mock.calls.map(([path]) => path)).toEqual(['/providers', '/providers/rice-lab/members'])
  })

  it('shows an ordinary user only their own account and instance roles', async () => {
    useAuth.mockReturnValue({
      user: { email: 'signed-in@example.test', live_provider_access: true },
      email: 'signed-in@example.test',
      hasPermission: () => false,
      isSuperAdmin: false,
      isGlobalViewer: false,
      instances: ['rice-lab'],
      stateRoles: { 'rice-lab': 'state_contributor' },
    })
    const dismissProfile = vi.fn()
    render(
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<UserAccessSheet open onOpenChange={dismissProfile} />} />
          <Route path="/users" element={<UsersAdminView />} />
        </Routes>
      </MemoryRouter>,
    )
    await userEvent.setup().click(screen.getByRole('link', { name: 'Your access' }))

    expect(dismissProfile).toHaveBeenCalledWith(false)
    expect(screen.getByRole('heading', { name: 'Your access' })).toBeVisible()
    expect(screen.getByText('Signed in as signed-in@example.test.')).toBeVisible()
    expect(screen.getByText('RICE-LAB')).toBeVisible()
    expect(screen.getByText('Contributor')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add user' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add instance' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Grant access' })).not.toBeInTheDocument()
    expect(fetchJson).not.toHaveBeenCalled()
  })
})
