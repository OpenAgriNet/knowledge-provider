import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import SearchWorkbenchView from './SearchWorkbenchView'
import { DEFAULT_SEARCH_SETTINGS, fetchJson } from '../lib/pipelineUi'

vi.mock('../lib/pipelineUi', async importOriginal => ({ ...(await importOriginal()), fetchJson: vi.fn() }))

it('keeps network scope and omits diagnostics on search and scheme selection', async () => {
  fetchJson.mockImplementation(async (path, options) => {
    if (path === '/settings/search') return DEFAULT_SEARCH_SETTINGS
    if (path === '/search/collections') return [{ name: 'published', label: 'Published', network_id: 'a', document_kind: 'scheme' }]
    const request = JSON.parse(options.body)
    return { hits: [], scheme_choices: request.document_workflow_id ? [] : [
      { workflow_id: 'first', code: 'pk', name: 'PM Kisan' }, { workflow_id: 'second', code: 'kcc', name: 'KCC' },
    ] }
  })
  render(<MemoryRouter><SearchWorkbenchView /></MemoryRouter>)
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith('/search/collections'))
  fireEvent.click(screen.getByRole('button', { name: 'eligibility criteria' }))
  fireEvent.click(await screen.findByRole('button', { name: 'PM Kisan (pk)' }))
  await waitFor(() => expect(fetchJson.mock.calls.filter(([path]) => path === '/search')).toHaveLength(2))
  const requests = fetchJson.mock.calls.filter(([path]) => path === '/search').map(([, options]) => JSON.parse(options.body))
  expect(requests[0]).toMatchObject({ network_id: 'a', document_kind: 'scheme', include_raw_hits: false })
  expect(requests[1]).toMatchObject({ network_id: 'a', document_workflow_id: 'first', include_raw_hits: false })
  expect(screen.queryByRole('button', { name: /candidate hits/ })).not.toBeInTheDocument()
})
