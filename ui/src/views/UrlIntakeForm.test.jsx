import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import UrlIntakeForm from './UrlIntakeForm'
import { fetchJson } from '../lib/pipelineUi'

vi.mock('../lib/pipelineUi', () => ({ fetchJson: vi.fn() }))
beforeEach(() => fetchJson.mockReset())

it('submits a public URL without any saved source', async () => {
  fetchJson.mockResolvedValueOnce({ sources: [] }).mockResolvedValueOnce({ workflow_id: 'job-1', status: 'running' })
  const queued = vi.fn()
  render(<MemoryRouter><UrlIntakeForm provider="example" kind="advisory" onQueued={queued} /></MemoryRouter>)
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Source URL'), 'https://notices.example/notice')
  await user.click(screen.getByRole('button', { name: 'Ingest URL' }))
  expect(fetchJson).toHaveBeenLastCalledWith('/providers/example/crawl', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ url: 'https://notices.example/notice', kind: 'advisory' }),
  }))
  expect(await screen.findByRole('link', { name: 'Open document' })).toHaveAttribute('href', '/documents/job-1')
  expect(queued).toHaveBeenCalledOnce()
})

it('keeps a saved source request on its policy-specific route', async () => {
  fetchJson.mockResolvedValueOnce({ sources: [{ id: 'bulletins', host: 'notices.example' }] }).mockResolvedValueOnce({ items: [] })
  render(<MemoryRouter><UrlIntakeForm provider="example" kind="advisory" onQueued={() => {}} /></MemoryRouter>)
  const user = userEvent.setup()
  await screen.findByRole('option', { name: 'bulletins (notices.example)' })
  await user.selectOptions(screen.getByLabelText('Access'), 'bulletins')
  await user.type(screen.getByLabelText('Source URL'), 'https://notices.example/notice')
  await user.click(screen.getByRole('button', { name: 'Ingest URL' }))
  expect(fetchJson).toHaveBeenLastCalledWith('/providers/example/crawl/bulletins', expect.objectContaining({ method: 'POST' }))
})
