import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { fetchJson } from '../lib/pipelineUi'
import RegistryProviderPanel from './RegistryProviderPanel'

vi.mock('../lib/pipelineUi', async importOriginal => ({ ...await importOriginal(), fetchJson: vi.fn() }))

const digest = 'a'.repeat(64)
const base = '/providers/rice/networks/dev.test'
const context = (overrides = {}) => ({
  network_id: 'dev.test', participant_id: 'rice.dev.test',
  schema_packs: [{ capability: 'openagrinet:KnowledgeAdvisory', digest, version: 'v1' }],
  admission: { expected_version: 0, current: null, latest_proposal: null }, route: null,
  ...overrides,
})

beforeEach(() => { fetchJson.mockReset() })

function respondToReads(readContext = context()) {
  fetchJson.mockImplementation(async path => {
    if (path.endsWith('/registry-networks')) return { networks: [{ network_id: 'dev.test', participant_id: 'rice.dev.test' }] }
    if (path.includes('/registry-context')) return readContext
    throw new Error(`Unexpected request: ${path}`)
  })
}

async function openPanel() {
  const user = userEvent.setup()
  render(<RegistryProviderPanel provider="rice" />)
  expect(fetchJson).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Load Registry connections' }))
  await screen.findByLabelText('Participant name')
  return user
}

it('separates the observed admission from a locally approved proposal', async () => {
  respondToReads(context({ admission: {
    expected_version: 2,
    current: { record_id: 'remote-1', revision: 'r1', name: 'Rice', base_url: 'https://rice.test', status: 'inactive' },
    latest_proposal: { version: 2, change: { name: 'Rice', base_url: 'https://rice.test', status: 'active' }, decision: { outcome: 'approved', reason: 'Reviewed' } },
  } }))
  await openPanel()
  expect(screen.getByText('Live Registry: inactive.')).toBeVisible()
  expect(screen.getByText('Local proposal 2: approved.')).toBeVisible()
  expect(screen.getByText(/they do not update the live Registry/)).toBeVisible()
  expect(screen.getByLabelText('Requested status')).toHaveValue('active')
})

it('preserves the request identity after an uncertain failure and binds observed versions', async () => {
  const bodies = []
  fetchJson.mockImplementation(async (path, options) => {
    if (path.endsWith('/registry-networks')) return { networks: [{ network_id: 'dev.test' }] }
    if (path.includes('/registry-context')) return context({ admission: {
      expected_version: 4, current: { record_id: 'remote-1', revision: 'revision-7', name: 'Rice', base_url: 'https://rice.test', status: 'active' }, latest_proposal: null,
    } })
    if (path === `${base}/registry-proposals`) {
      const body = JSON.parse(options.body)
      bodies.push(body)
      if (bodies.length === 1) throw new Error('Connection interrupted.')
      return { version: 5, change: body.change, decision: null }
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  const user = await openPanel()
  await user.type(screen.getByLabelText('Reason for change'), 'Confirm participant')
  await user.click(screen.getByRole('button', { name: 'Submit admission proposal' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Connection interrupted')
  await user.click(screen.getByRole('button', { name: 'Submit admission proposal' }))
  expect(await screen.findByText(/Proposal submitted for operator review/)).toBeVisible()
  expect(bodies).toHaveLength(2)
  expect(bodies[1]).toEqual(bodies[0])
  expect(bodies[0]).toMatchObject({ expected_version: 4, observed_record: { record_id: 'remote-1', revision: 'revision-7' }, change: { name: 'Rice', base_url: 'https://rice.test', status: 'active' } })
  expect(bodies[0].request_id).toMatch(/^[0-9a-f-]{36}$/)
})

it('submits route identity and schema pin from the selected provider and capability', async () => {
  let body
  fetchJson.mockImplementation(async (path, options) => {
    if (path.endsWith('/registry-networks')) return { networks: [{ network_id: 'dev.test' }] }
    if (path.includes('/registry-context')) return context({
      admission: { expected_version: 1, current: { record_id: 'p1', revision: 'r1', name: 'Rice', base_url: 'https://rice.test', status: 'active' }, latest_proposal: null },
      route: path.includes('?capability=') ? { expected_version: 0, current: null, latest_proposal: null } : null,
    })
    if (path === `${base}/registry-proposals`) { body = JSON.parse(options.body); return { version: 1, change: body.change } }
    throw new Error(`Unexpected request: ${path}`)
  })
  const user = await openPanel()
  await user.selectOptions(screen.getByLabelText('Route capability'), 'openagrinet:KnowledgeAdvisory')
  const form = await screen.findByRole('form', { name: 'Submit route proposal' })
  await user.type(within(form).getByLabelText('Mapping URL'), 'https://mapping.test/advisory.select.yaml')
  await user.type(within(form).getByLabelText('Mapping SHA-256'), 'b'.repeat(64))
  await user.type(within(form).getByLabelText('Mapping runtime version'), 'jsonata@1.5.4')
  await user.type(within(form).getByLabelText('Authentication profile'), 'rice-search')
  await user.type(within(form).getByLabelText('Reason for change'), 'Expose published advisories')
  await user.click(within(form).getByRole('button', { name: 'Submit route proposal' }))
  await screen.findByText(/Proposal submitted for operator review/)
  expect(body).toMatchObject({ expected_version: 0, observed_record: null, change: {
    schema_pack_digest: digest, status: 'active', route: {
      network_id: 'dev.test', participant_id: 'rice.dev.test', capability: 'openagrinet:KnowledgeAdvisory',
      base_url: 'https://rice.test', action: 'select', method: 'POST', path: '/search',
      mapping_sha256: 'b'.repeat(64), mapping_runtime: 'jsonata@1.5.4', auth_profile: 'rice-search', timeout_ms: 15000, retry_max: 0,
    },
  } })
})

it('uses a new request identity when a failed proposal is edited', async () => {
  const submitted = []
  fetchJson.mockImplementation(async (path, options) => {
    if (path.endsWith('/registry-networks')) return { networks: [{ network_id: 'dev.test' }] }
    if (path.includes('/registry-context')) return context()
    submitted.push(JSON.parse(options.body))
    throw new Error('Request failed.')
  })
  const user = await openPanel()
  await user.type(screen.getByLabelText('Participant name'), 'Rice')
  await user.type(screen.getByLabelText('Provider base URL'), 'https://rice.test')
  await user.type(screen.getByLabelText('Reason for change'), 'Register provider')
  await user.click(screen.getByRole('button', { name: 'Submit admission proposal' }))
  await screen.findByRole('alert')
  await user.type(screen.getByLabelText('Participant name'), ' research')
  await user.click(screen.getByRole('button', { name: 'Submit admission proposal' }))
  await waitFor(() => expect(submitted).toHaveLength(2))
  expect(submitted[1].request_id).not.toBe(submitted[0].request_id)
  expect(submitted[1].change.name).toBe('Rice research')
})

it('discards an old network response after the network changes', async () => {
  let finishFirst
  fetchJson.mockImplementation(path => {
    if (path.endsWith('/registry-networks')) return Promise.resolve({ networks: [{ network_id: 'dev.test' }, { network_id: 'other.test' }] })
    if (path.includes('/networks/dev.test/')) return new Promise(resolve => { finishFirst = resolve })
    return Promise.resolve(context({ network_id: 'other.test', participant_id: 'rice.other.test' }))
  })
  const user = userEvent.setup()
  render(<RegistryProviderPanel provider="rice" />)
  await user.click(screen.getByRole('button', { name: 'Load Registry connections' }))
  await user.selectOptions(await screen.findByLabelText('Registry network'), 'other.test')
  expect(await screen.findByText('Participant: rice.other.test')).toBeVisible()
  finishFirst(context())
  await waitFor(() => expect(screen.queryByText('Participant: rice.dev.test')).not.toBeInTheDocument())
})

it('clears provider records on the keyed provider switch', async () => {
  respondToReads()
  const user = userEvent.setup()
  const { rerender } = render(<RegistryProviderPanel key="rice" provider="rice" />)
  await user.click(screen.getByRole('button', { name: 'Load Registry connections' }))
  await screen.findByText('Participant: rice.dev.test')
  rerender(<RegistryProviderPanel key="cotton" provider="cotton" />)
  expect(screen.queryByText('Participant: rice.dev.test')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Load Registry connections' })).toBeVisible()
  expect(fetchJson.mock.calls.some(([path]) => path.includes('/providers/cotton'))).toBe(false)
})
