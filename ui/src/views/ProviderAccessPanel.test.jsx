import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { fetchJson } from '../lib/pipelineUi'
import ProviderAccessPanel from './ProviderAccessPanel'

vi.mock('../lib/pipelineUi', async importOriginal => ({ ...await importOriginal(), fetchJson: vi.fn() }))

beforeEach(() => {
  fetchJson.mockReset()
})

it('revokes only the member of the instance selected by an instance administrator', async () => {
  let revoked = false
  fetchJson.mockImplementation(async (path, options) => {
    if (path === '/providers') return { providers: [{ id: 'rice-id', provider: 'rice-lab' }] }
    if (path === '/providers/rice-lab/members') return { members: revoked ? [] : [
      { user_id: 'member', email: 'member@example.test', enabled: true, role: 'state_view' },
    ] }
    if (path === '/providers/rice-lab/members/member' && options.method === 'DELETE') {
      revoked = true
      return {}
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  const user = userEvent.setup()
  render(<ProviderAccessPanel isSuperAdmin={false} />)

  expect(await screen.findByText('member@example.test')).toBeVisible()
  expect(within(screen.getByLabelText('Instance role for member@example.test')).queryByRole('option', { name: 'Instance administrator' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add instance' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Revoke', exact: true }))
  expect(await screen.findByRole('alertdialog')).toHaveTextContent('Revoke access to RICE-LAB?')
  await user.click(screen.getByRole('button', { name: 'Revoke access' }))

  await screen.findByText('Instance access revoked.')
  await waitFor(() => expect(screen.queryByText('member@example.test')).not.toBeInTheDocument())
  expect(fetchJson).toHaveBeenCalledWith('/providers/rice-lab/members/member', { method: 'DELETE' })
})

it('shows scheduled feed health for the selected instance and clears it on switch', async () => {
  fetchJson.mockImplementation(async path => {
    if (path === '/providers') return { providers: [
      { id: 'first-id', provider: 'first' }, { id: 'second-id', provider: 'second' },
    ] }
    if (path.endsWith('/members')) return { members: [] }
    if (path === '/providers/first/scheduled-sources') return { checked_at: '2026-11-12T01:05:00Z', sources: [{
      source_key: 'notices', status: 'retrying', consecutive_failures: 2, auto_release: false,
      last_page_received_at: '2026-11-12T01:00:00Z', next_check_at: '2026-11-12T01:30:00Z',
    }, {
      source_key: 'bulletins', status: 'scheduled', consecutive_failures: 0, auto_release: true,
      release_network_id: 'secondary.test', last_page_policy_conflicts: 3,
    }] }
    if (path === '/providers/second/scheduled-sources') return { checked_at: '2026-11-12T01:05:00Z', sources: [] }
    throw new Error(`Unexpected request: ${path}`)
  })
  const user = userEvent.setup()
  render(<ProviderAccessPanel isSuperAdmin={false} />)
  await user.click(await screen.findByRole('tab', { name: 'Intake' }))
  const panel = await screen.findByRole('region', { name: 'Scheduled intake' })
  await user.click(within(panel).getByRole('button', { name: 'Check status' }))
  expect(await within(panel).findByText('Waiting to retry')).toBeVisible()
  expect(within(panel).getByText(/Checked/)).toBeVisible()
  expect(within(panel).getByText('Draft only')).toBeVisible()
  expect(within(panel).getByText('Automatic release to secondary.test')).toBeVisible()
  expect(within(panel).getByText('Consecutive failures 2')).toBeVisible()
  expect(within(panel).getByText('Last page kept the earlier release policy for 3 items')).toBeVisible()
  expect(within(panel).getByText(/Next check/)).toBeVisible()

  await user.selectOptions(screen.getByLabelText('Instance'), 'second')
  expect(screen.queryByText('notices')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Check status' }))
  expect(await screen.findByText('No scheduled sources configured.')).toBeVisible()
})
