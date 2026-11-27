import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { fetchJson } from '../lib/pipelineUi'
import WebhookSourcePolicy from './WebhookSourcePolicy'

vi.mock('../lib/pipelineUi', () => ({ fetchJson: vi.fn() }))

const DIRECTORY = '/knowledge/intake/rice-lab'
const POLICY_PATH = '/knowledge/intake/rice-lab/crop-advisories'
const SAMPLE = { advisories: [{ advisoryId: 5, title: 'Field care', content: { text: 'Check drainage', language: 'en' } }] }
const MAPPING = {
  records: '/advisories',
  fields: { external_id: '/advisoryId', title: '/title', text: '/content/text', language: '/content/language' },
}
const EXISTING = {
  version: 3, service_client: 'field-service', service_subject: 'service-account-5',
  enabled: true, auto_release: false, mapping: MAPPING,
}

let request
beforeEach(() => {
  request = vi.fn()
  fetchJson.mockImplementation((path, options) => {
    if (path === DIRECTORY) return Promise.resolve({ sources: [], index_only: false })
    if (path === '/providers/rice-lab/source-settings') return Promise.resolve({ templates: { 'notice-feed': MAPPING } })
    return request(path, options)
  })
})

async function openPolicy(user, action) {
  render(<WebhookSourcePolicy provider="rice-lab" />)
  await user.type(screen.getByLabelText('Source key'), 'crop-advisories')
  await user.click(screen.getByRole('button', { name: action }))
}

it('suggests field paths from a sample and pins a provider-scoped version', async () => {
  request.mockResolvedValue({ ...EXISTING, version: 1, auto_release: true, release_network_id: 'partner.test' })
  const user = userEvent.setup()
  await openPolicy(user, 'New policy')

  expect(screen.getByLabelText('Submission URL').value).toContain(`${POLICY_PATH}/submissions`)
  await user.type(screen.getByLabelText('Keycloak service client ID'), 'field-service')
  fireEvent.change(screen.getByLabelText('Sample submission JSON'), { target: { value: JSON.stringify(SAMPLE) } })
  await user.click(screen.getByRole('button', { name: 'Suggest field paths' }))
  expect(JSON.parse(screen.getByLabelText('Mapping JSON').value)).toEqual(MAPPING)
  await user.click(screen.getByRole('checkbox', { name: 'Publish automatically' }))
  await user.type(screen.getByLabelText('Release network'), 'partner.test')
  await user.click(screen.getByRole('button', { name: 'Pin mapping' }))

  expect(await screen.findByRole('status')).toHaveTextContent('Pinned mapping version 1.')
  const [[path, options]] = request.mock.calls
  expect(path).toBe(POLICY_PATH)
  expect(options.method).toBe('PUT')
  expect(JSON.parse(options.body)).toEqual({
    expected_version: 0, service_client: 'field-service', service_subject: null,
    mapping: MAPPING, fixture: SAMPLE, enabled: true, auto_release: true, release_network_id: 'partner.test',
  })
})

it('uses the loaded version for an update and preserves the sample if the save conflicts', async () => {
  request.mockResolvedValueOnce(EXISTING).mockRejectedValueOnce(new Error('Webhook source policy changed. Load the current version and retry.'))
  const user = userEvent.setup()
  await openPolicy(user, 'Load policy')

  expect(await screen.findByText('crop-advisories · Version 3')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Sample submission JSON'), { target: { value: JSON.stringify(SAMPLE) } })
  await user.click(screen.getByRole('button', { name: 'Pin mapping' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Load the current version and retry.')
  expect(screen.getByLabelText('Sample submission JSON')).toHaveValue(JSON.stringify(SAMPLE))
  expect(request.mock.calls[0]).toEqual([POLICY_PATH, undefined])
  expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({
    expected_version: 3, service_subject: 'service-account-5', mapping: MAPPING,
  })
})

it('flags malformed fixture JSON before saving', async () => {
  const user = userEvent.setup()
  await openPolicy(user, 'New policy')
  await user.type(screen.getByLabelText('Keycloak service client ID'), 'field-service')
  fireEvent.change(screen.getByLabelText('Sample submission JSON'), { target: { value: '{' } })
  await user.click(screen.getByRole('button', { name: 'Pin mapping' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Sample submission must be valid JSON.')
  expect(request).not.toHaveBeenCalled()
})

it.each([
  ['canonical', { external_id: '1', title: 'Notice', text: 'Full text' }, { runtime: 'canonical-v1' }],
  ['template', SAMPLE, MAPPING],
])('pins a %s policy without sending an inline mapping', async (mode, fixture, pinnedMapping) => {
  request.mockResolvedValue({ ...EXISTING, version: 1, mapping: pinnedMapping })
  const user = userEvent.setup()
  await openPolicy(user, 'New policy')
  await user.type(screen.getByLabelText('Keycloak service client ID'), 'field-service')
  await user.selectOptions(screen.getByLabelText('Payload format'), mode)
  if (mode === 'template') {
    const field = screen.getByLabelText('Template name')
    expect(document.getElementById(field.getAttribute('list')).options[0].value).toBe('notice-feed')
    await user.type(field, 'notice-feed')
  }
  fireEvent.change(screen.getByLabelText('Sample submission JSON'), { target: { value: JSON.stringify(fixture) } })
  await user.click(screen.getByRole('button', { name: 'Pin mapping' }))

  expect(await screen.findByRole('status')).toHaveTextContent('Pinned mapping version 1.')
  const body = JSON.parse(request.mock.calls[0][1].body)
  expect(body.fixture).toEqual(fixture)
  expect(body.mapping).toBeUndefined()
  expect(body.template).toBe(mode === 'template' ? 'notice-feed' : undefined)
})
