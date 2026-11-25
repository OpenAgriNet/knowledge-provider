import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import MetadataReview from './MetadataReview'
import { fetchJson } from '../lib/pipelineUi'

vi.mock('../lib/pipelineUi', () => ({ fetchJson: vi.fn() }))

it('edits a supplied category as text when the schema has no enum', async () => {
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'a'.repeat(64), category_options: [], supplied: [{ field: 'category', value: 'Incident response' }],
  }).mockResolvedValueOnce({ version: 'b'.repeat(64), accepted: { facts: [{ field: 'category', value: 'Emergency advice' }] } })
  render(<MetadataReview workflowId="doc-category" canEdit />)
  const details = screen.getByText('Document metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  const field = await screen.findByLabelText('Category 1')
  expect(field.value).toBe('Incident response')
  fireEvent.change(field, { target: { value: 'Emergency advice' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith('/documents/doc-category/metadata-review', expect.objectContaining({
    method: 'PUT', body: expect.stringContaining('Emergency advice'),
  })))
})

it('generates and accepts only the selected fields for the requested chunk', async () => {
  const base = '/documents/doc-1/metadata-review'
  const facts = [
    { field: 'crop', value: 'Wheat', evidence: { quote: 'Wheat field advisory' } },
    { field: 'season', value: 'Rabi', evidence: { quote: 'Rabi season' } },
  ]
  fetchJson.mockReset().mockResolvedValueOnce({}).mockResolvedValueOnce({
    pending: { id: 'proposal-1', facts },
  }).mockResolvedValueOnce({ accepted: { facts: [facts[0]] } })
  render(<MetadataReview workflowId="doc-1" chunkNumber={2} canEdit />)
  const details = screen.getByText('Chunk metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith(base + '?chunk_number=2', expect.anything()))
  fireEvent.click(screen.getByRole('button', { name: 'Generate metadata' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /Season/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Accept selected' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith(base + '/accept?chunk_number=2', expect.objectContaining({
    method: 'POST',
    body: JSON.stringify({ proposal_id: 'proposal-1', selected: [0] }),
  })))
  expect(await screen.findByRole('list', { name: 'Accepted metadata' })).toHaveTextContent('Wheat')
})

it.each([undefined, 2])('edits and renders saved metadata for scope %s', async chunkNumber => {
  const base = '/documents/doc-1/metadata-review'
  const suffix = chunkNumber == null ? '' : '?chunk_number=2'
  const evidence = { block: 1, start: 0, end: 5, quote: 'Wheat' }
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'test-version',
    pending: { id: 'proposal-error', facts: [{ field: 'crop', value: 'Wheat', evidence }] },
  }).mockResolvedValueOnce({
    version: 'new-version',
    accepted: { overrides: ['crop'], facts: [{ field: 'crop', value: 'Maize' }] },
  })
  render(<MetadataReview workflowId="doc-1" chunkNumber={chunkNumber} canEdit />)
  const details = screen.getByText(chunkNumber == null ? 'Document metadata' : 'Chunk metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  fireEvent.click(await screen.findByRole('button', { name: 'Edit suggestions' }))
  fireEvent.change(screen.getByLabelText('Crop 1'), { target: { value: 'Maize' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith(base + suffix, expect.objectContaining({
    method: 'PUT',
    body: JSON.stringify({ version: 'test-version', overrides: ['crop'], facts: [{ field: 'crop', value: 'Maize', evidence }] }),
  })))
  expect(await screen.findByLabelText('Crop 1')).toHaveValue('Maize')
})

it('edits document validity together with other metadata', async () => {
  const path = '/documents/doc-1/metadata-review'
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'test-version',
    validity: { startsAt: '2027-10-01T00:00:00Z' },
    validity_days: { valid_from: '2027-10-01', valid_to: null },
  }).mockResolvedValueOnce({
    version: 'new-version',
    validity: { startsAt: '2027-10-01T00:00:00Z', endsAt: '2028-02-15T23:59:59.999999Z' },
    validity_days: { valid_from: '2027-10-01', valid_to: '2028-02-15' },
    accepted: { facts: [], overrides: [] },
  })
  render(<MetadataReview workflowId="doc-1" canEdit />)
  const details = screen.getByText('Document metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  fireEvent.change(await screen.findByLabelText('End date'), { target: { value: '2028-02-15' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith(path, expect.objectContaining({ method: 'PUT' })))
  const request = fetchJson.mock.calls.find(([, options]) => options.method === 'PUT')
  expect(JSON.parse(request[1].body).validity).toEqual({ startsAt: '2027-10-01T00:00:00Z', endsAt: '2028-02-15' })
  expect(await screen.findByLabelText('End date')).toHaveValue('2028-02-15')
})


it('opens directly on chunk fields and inherits after removing the last local value', async () => {
  const path = '/documents/doc-1/metadata-review?chunk_number=2'
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'test-version',
    inherited: [{ field: 'category', value: 'Crop' }, { field: 'crop', value: 'Wheat' }],
    accepted: { overrides: ['crop'], facts: [{ field: 'crop', value: 'Maize' }] },
  }).mockResolvedValueOnce({
    version: 'updated-version', accepted: { overrides: [], facts: [] },
  })
  render(<MetadataReview workflowId="doc-1" chunkNumber={2} canEdit />)
  const details = screen.getByText('Chunk metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))

  expect(await screen.findByLabelText('Crop 1')).toHaveValue('Maize')
  expect(screen.queryByRole('combobox', { name: 'Category 1' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Add category' })).toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: /override/i })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Remove crop 1' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith(path, expect.objectContaining({
    method: 'PUT',
    body: JSON.stringify({ version: 'test-version', overrides: [], facts: [] }),
  })))
  expect(screen.queryByRole('textbox', { name: 'Crop 1' })).not.toBeInTheDocument()
})

it('shows supplied document values and saves only edited fields as reviewer values', async () => {
  const path = '/documents/doc-1/metadata-review'
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'test-version',
    supplied: [{ field: 'category', value: 'Crop' }, { field: 'crop', value: 'Wheat' }],
    category_options: ['Crop', 'Market'],
  }).mockResolvedValueOnce({
    version: 'updated-version', accepted: { overrides: ['crop'], facts: [{ field: 'crop', value: 'Maize' }] },
  })
  render(<MetadataReview workflowId="doc-1" canEdit />)
  const details = screen.getByText('Document metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))

  expect(await screen.findByRole('combobox', { name: 'Category 1' })).toHaveValue('Crop')
  expect(screen.getByLabelText('Crop 1')).toHaveValue('Wheat')
  fireEvent.change(screen.getByLabelText('Crop 1'), { target: { value: 'Maize' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith(path, expect.objectContaining({
    method: 'PUT',
    body: JSON.stringify({ version: 'test-version', overrides: ['crop'], facts: [{ field: 'crop', value: 'Maize' }] }),
  })))
})


it('starts a new category with a placeholder when no category was recorded', async () => {
  fetchJson.mockReset().mockResolvedValueOnce({ version: 'test-version', supplied: [], category_options: ['Crop', 'Market'] })
  render(<MetadataReview workflowId="untagged" canEdit />)
  const details = screen.getByText('Document metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  fireEvent.click(await screen.findByRole('button', { name: 'Add category' }))
  expect(screen.getByRole('combobox', { name: 'Category 1' })).toHaveValue('')
  expect(screen.getByRole('option', { name: 'Market' })).toBeInTheDocument()
})

it('can clear a chunk field explicitly and then return to the document value', async () => {
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'first', inherited: [{ field: 'crop', value: 'Wheat' }],
  }).mockResolvedValueOnce({
    version: 'second', accepted: { overrides: ['crop'], facts: [] },
  }).mockResolvedValueOnce({ version: 'third', accepted: { overrides: [], facts: [] } })
  render(<MetadataReview workflowId="clear-test" chunkNumber={1} canEdit />)
  const details = screen.getByText('Chunk metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  fireEvent.click(await screen.findByRole('button', { name: 'Clear crop for this chunk' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(1))
  let requests = fetchJson.mock.calls.filter(([, options]) => options.method === 'PUT')
  expect(JSON.parse(requests[0][1].body)).toMatchObject({ overrides: ['crop'], facts: [] })
  expect(await screen.findByText('Cleared for this chunk')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Use document crop' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(2))
  requests = fetchJson.mock.calls.filter(([, options]) => options.method === 'PUT')
  expect(JSON.parse(requests[1][1].body)).toMatchObject({ overrides: [], facts: [] })
})

it('suggests current and schema season values and allows a new local season', async () => {
  fetchJson.mockReset().mockResolvedValueOnce({
    version: 'test-version',
    supplied: [{ field: 'season', value: 'Local dry season' }],
    pending: { id: 'proposal', facts: [{ field: 'season', value: 'rabi' }] },
  }).mockResolvedValueOnce({
    version: 'saved-version',
    accepted: { facts: [{ field: 'season', value: 'Locally named season' }], overrides: ['season'] },
  })
  render(<MetadataReview workflowId="doc-suggestions" canEdit />)
  const details = screen.getByText('Document metadata').closest('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  const season = await screen.findByRole('combobox', { name: 'Season 1' })
  expect([...season.list.options].map(option => option.value)).toEqual(
    ['Kharif', 'Rabi', 'Navarai', 'Samba', 'Local dry season'],
  )
  fireEvent.change(season, { target: { value: 'Locally named season' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }))
  await waitFor(() => expect(fetchJson).toHaveBeenCalledWith('/documents/doc-suggestions/metadata-review',
    expect.objectContaining({ method: 'PUT', body: expect.stringContaining('Locally named season') })))
})
