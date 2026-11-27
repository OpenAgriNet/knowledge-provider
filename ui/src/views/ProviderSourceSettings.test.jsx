import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import ProviderSourceSettings from './ProviderSourceSettings'
import { fetchJson } from '../lib/pipelineUi'

vi.mock('../lib/pipelineUi', () => ({ fetchJson: vi.fn() }))
beforeEach(() => fetchJson.mockReset())

it('saves provider source settings with the version that was loaded', async () => {
  const initial = { version: 3, templates: {}, crawl: {}, schedules: [] }
  fetchJson.mockResolvedValueOnce(initial).mockResolvedValueOnce({ ...initial, version: 4 })
  render(<ProviderSourceSettings provider="example" />)
  const user = userEvent.setup()
  await user.click(screen.getByText('Edit source settings'))
  await user.click(screen.getByRole('button', { name: 'Save source settings' }))
  expect(fetchJson).toHaveBeenLastCalledWith('/providers/example/source-settings', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ templates: {}, crawl: {}, schedules: [], version: 3 }),
  }))
  expect(await screen.findByRole('status')).toHaveTextContent('Source settings saved')
})

it('keeps the edited settings when the server reports a version conflict', async () => {
  fetchJson.mockResolvedValueOnce({ version: 1, templates: {}, crawl: {}, schedules: [] })
    .mockRejectedValueOnce(new Error('Source settings changed. Reload before saving.'))
  render(<ProviderSourceSettings provider="example" />)
  const user = userEvent.setup()
  await user.click(screen.getByText('Edit source settings'))
  const draft = await screen.findByLabelText('Source settings JSON')
  await user.click(screen.getByRole('button', { name: 'Save source settings' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Reload before saving')
  expect(draft).toHaveValue(JSON.stringify({ templates: {}, crawl: {}, schedules: [] }, null, 2))
})

it('previews a JSONata sample and saves the template under its name', async () => {
  const settings = { version: 3, templates: {}, crawl: {}, schedules: [] }
  const mapping = { runtime: 'jsonata-v1', expression: 'payload' }
  fetchJson.mockResolvedValueOnce(settings)
    .mockResolvedValueOnce({ documents: [{ external_id: '991:823' }] })
    .mockResolvedValueOnce({ ...settings, version: 4, templates: { 'icar-advisory': mapping } })
  render(<ProviderSourceSettings provider="icar" />)
  const user = userEvent.setup()
  await user.click(screen.getByText('Create or edit a JSONata template'))
  await user.type(screen.getByLabelText('Template name'), 'icar-advisory')
  await user.type(screen.getByLabelText('JSONata expression'), 'payload')
  await user.click(screen.getByLabelText('Sample input JSON'))
  await user.paste('{"payload":{"external_id":"991:823"}}')
  expect(screen.getByRole('button', { name: 'Save template' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Run preview' }))
  expect(await screen.findByText('1 document mapped')).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Canonical JSON output' })).toHaveTextContent('991:823')
  await user.click(screen.getByRole('button', { name: 'Save template' }))
  expect(fetchJson).toHaveBeenLastCalledWith('/providers/icar/source-settings', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ templates: { 'icar-advisory': mapping }, crawl: {}, schedules: [], version: 3 }),
  }))
})
