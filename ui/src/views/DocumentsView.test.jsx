import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeAll, expect, it, vi } from 'vitest'
import DocumentsView from './DocumentsView'

vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ hasPermission: () => false, instances: ['new-provider', 'existing'] }),
}))

vi.mock('../lib/pipelineUi', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchAllDocuments: vi.fn().mockResolvedValue([
    { workflow_id: 'wf-existing', name: 'Existing document', instance: 'existing', stage: 'completed' },
  ]),
  fetchJson: vi.fn().mockResolvedValue({ providers: ['new-provider', 'existing'] }),
}))

beforeAll(() => {
  window.HTMLElement.prototype.hasPointerCapture ??= () => false
  window.HTMLElement.prototype.releasePointerCapture ??= () => {}
  window.HTMLElement.prototype.scrollIntoView ??= () => {}
})

it('lets a scoped user filter by every readable instance, including one without documents', async () => {
  const user = userEvent.setup()
  render(<MemoryRouter><DocumentsView /></MemoryRouter>)

  expect(await screen.findByText('Existing document')).toBeInTheDocument()
  await user.click(await screen.findByRole('combobox', { name: 'Instance' }))
  expect(screen.getByRole('option', { name: 'EXISTING' })).toBeInTheDocument()
  await user.click(screen.getByRole('option', { name: 'NEW-PROVIDER' }))
  expect(screen.getByText('No documents match the current filters.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Clear filters' }))
  expect(screen.getByText('Existing document')).toBeInTheDocument()
})
