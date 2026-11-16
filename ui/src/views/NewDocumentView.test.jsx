import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import NewDocumentView from './NewDocumentView'
import { fetchJson } from '../lib/pipelineUi'

const apiFetch = vi.fn()
const defaultAuth = {
  hasPermission: () => true,
  hasPermissionForInstance: () => true,
  instances: ['mh'],
  isSuperAdmin: false,
  user: {},
}
let auth = defaultAuth

vi.mock('../auth/keycloak', () => ({ apiFetch: (...args) => apiFetch(...args) }))
vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => auth,
}))
vi.mock('../lib/pipelineUi', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchJson: vi.fn().mockResolvedValue([]),
}))

function renderView() {
  return render(
    <MemoryRouter>
      <NewDocumentView />
    </MemoryRouter>,
  )
}

async function pickFile(user, name = 'PM Kisan Guidelines.pdf') {
  const input = document.querySelector('input[type="file"]')
  await user.upload(input, new File(['%PDF-1.4'], name, { type: 'application/pdf' }))
}

// Radix Select needs these browser APIs, which jsdom lacks.
beforeAll(() => {
  window.HTMLElement.prototype.hasPointerCapture ??= () => false
  window.HTMLElement.prototype.releasePointerCapture ??= () => {}
  window.HTMLElement.prototype.scrollIntoView ??= () => {}
})

async function pickKind(user, label = 'Advisory') {
  await user.click(screen.getByRole('combobox', { name: /document type/i }))
  await user.click(await screen.findByRole('option', { name: label }))
}

describe('NewDocumentView display name', () => {
  beforeEach(() => {
    auth = defaultAuth
    apiFetch.mockReset()
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ workflow_id: 'wf-1' }) })
    fetchJson.mockReset()
    fetchJson.mockImplementation(async (path) => {
      if (path === '/upload/providers') return { providers: ['mh'] }
      if (path.endsWith('/crawl')) return { sources: [] }
      return []
    })
  })

  it('shows no display name field before a file is picked', () => {
    renderView()
    expect(screen.queryByLabelText(/display name/i)).not.toBeInTheDocument()
  })

  it('prefills the display name with the filename without its extension', async () => {
    const user = userEvent.setup()
    renderView()
    await pickFile(user)
    expect(screen.getByLabelText(/display name/i)).toHaveValue('PM Kisan Guidelines')
  })

  it('sends the edited display name with the upload', async () => {
    const user = userEvent.setup()
    renderView()
    await pickFile(user)
    await pickKind(user)
    const field = screen.getByLabelText(/display name/i)
    await user.clear(field)
    await user.type(field, 'Cotton Pest Advisory')
    await user.click(screen.getByRole('button', { name: /start ingestion/i }))

    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    const body = apiFetch.mock.calls[0][1].body
    expect(body.get('display_name')).toBe('Cotton Pest Advisory')
  })

  it('sends a structured crop reference with the upload', async () => {
    const user = userEvent.setup()
    renderView()
    await pickFile(user)
    await pickKind(user)
    await user.click(screen.getByRole('button', { name: 'Add subject' }))
    expect(screen.getByRole('button', { name: /start ingestion/i })).toBeDisabled()
    const subjectId = 'https://taxonomy.openagrinet.global/crops/cotton'
    await user.type(screen.getByLabelText('Taxonomy ID 1'), subjectId)
    await user.type(screen.getByLabelText('Subject name 1'), 'Cotton')
    await user.type(screen.getByLabelText('Subject code 1'), 'COTTON')
    await user.click(screen.getByRole('button', { name: /start ingestion/i }))

    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    const body = apiFetch.mock.calls[0][1].body
    expect(JSON.parse(body.get('knowledge_metadata')).agriculture_subjects).toEqual([
      { subjectId, descriptor: { name: 'Cotton', code: 'COTTON' } },
    ])
  })

  it('uploads multiple PDFs together and shows each result', async () => {
    const user = userEvent.setup()
    apiFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        items: [
          { filename: 'rice.pdf', status: 202, document: { workflow_id: 'wf-rice' } },
          { filename: 'cotton.pdf', status: 400, detail: 'Invalid PDF' },
        ],
      }),
    })
    renderView()
    await user.upload(document.querySelector('input[type="file"]'), [
      new File(['%PDF-1.4'], 'rice.pdf', { type: 'application/pdf' }),
      new File(['invalid'], 'cotton.pdf', { type: 'application/pdf' }),
    ])
    await pickKind(user)
    await user.click(screen.getByRole('button', { name: /start ingestion/i }))

    expect(await screen.findByText('Upload results')).toBeInTheDocument()
    expect(screen.getByText('Queued')).toBeInTheDocument()
    expect(screen.getByText('Invalid PDF')).toBeInTheDocument()
    const [[url, request]] = apiFetch.mock.calls
    expect(url).toContain('/upload/batch?')
    expect(request.body.getAll('files').map(({ name }) => name)).toEqual(['rice.pdf', 'cotton.pdf'])
  })

  it('uses the portal instance when local auth bypass has no groups', async () => {
    const user = userEvent.setup()
    auth = { ...defaultAuth, instances: [], authEnabled: false, user: { portal_instance: 'bv' } }
    fetchJson.mockImplementation(async (path) => path === '/upload/providers'
      ? { providers: ['bv'], default_provider: 'bv' }
      : [])
    renderView()
    await pickFile(user)
    await pickKind(user)
    await user.click(screen.getByRole('button', { name: /start ingestion/i }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    expect(apiFetch.mock.calls[0][0]).toContain('instance=bv')
  })

  it('lets a super admin choose the instance before fetching its URL sources', async () => {
    const user = userEvent.setup()
    auth = { ...defaultAuth, isSuperAdmin: true, instances: [] }
    fetchJson.mockImplementation(async (path) => {
      if (path === '/upload/providers') return { providers: ['cotton-hub', 'rice-lab'] }
      if (path.endsWith('/crawl')) return { sources: [] }
      return []
    })

    renderView()
    await user.click(screen.getByRole('button', { name: 'Use a URL' }))
    expect(screen.queryByLabelText('Source URL')).not.toBeInTheDocument()
    await user.click(await screen.findByRole('combobox', { name: 'Instance' }))
    expect(screen.getByRole('option', { name: 'COTTON-HUB' })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'RICE-LAB' }))
    expect(await screen.findByLabelText('Source URL')).toBeInTheDocument()
    expect(fetchJson).toHaveBeenCalledWith('/providers/rice-lab/crawl', expect.anything())
  })

  it('keeps the instance visible when the user can upload to only one', async () => {
    const user = userEvent.setup()
    renderView()
    await user.click(screen.getByRole('button', { name: 'Use a URL' }))
    expect(await screen.findByRole('combobox', { name: 'Instance' })).toHaveTextContent('MH')
    expect(await screen.findByLabelText('Source URL')).toBeInTheDocument()
  })

  it('lets the user retry a failed instance lookup', async () => {
    const user = userEvent.setup()
    let failed = false
    fetchJson.mockImplementation(async (path) => {
      if (path !== '/upload/providers') return []
      if (failed) return { providers: ['mh'] }
      failed = true
      throw new Error('Provider list unavailable')
    })
    renderView()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load instances.')
    await user.click(screen.getByRole('button', { name: 'Retry loading instances' }))
    expect(await screen.findByRole('combobox', { name: 'Instance' })).toHaveTextContent('MH')
  })

  it('retries starting a saved document without resending its PDF', async () => {
    const user = userEvent.setup()
    apiFetch
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          workflow_id: 'wf-saved',
          error_message: 'Processing could not be confirmed.',
          available_actions: ['retry_upload_start'],
        }),
      })
      .mockResolvedValueOnce({ ok: true })
    renderView()
    await pickFile(user)
    await pickKind(user)
    await user.click(screen.getByRole('button', { name: /start ingestion/i }))
    expect(await screen.findByText('Document saved')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry start' }))
    expect(await screen.findByText('Document queued')).toBeInTheDocument()
    expect(apiFetch.mock.calls[1]).toEqual([
      expect.stringMatching(/\/upload\/wf-saved\/start$/),
      { method: 'POST' },
    ])
  })

  it('blocks upload when the display name is blank', async () => {
    const user = userEvent.setup()
    renderView()
    await pickFile(user)
    await user.clear(screen.getByLabelText(/display name/i))

    expect(screen.getByText(/cannot be blank/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start ingestion/i })).toBeDisabled()
  })

  it('blocks upload when the display name is too long', async () => {
    const user = userEvent.setup()
    renderView()
    await pickFile(user)
    const field = screen.getByLabelText(/display name/i)
    await user.clear(field)
    await user.click(field)
    await user.paste('a'.repeat(151))

    expect(screen.getByText(/at most 150/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start ingestion/i })).toBeDisabled()
  })

  it('resets the display name when the file is removed', async () => {
    const user = userEvent.setup()
    renderView()
    await pickFile(user)
    await user.click(screen.getByRole('button', { name: /remove/i }))
    await pickFile(user, 'Second File.docx')
    expect(screen.getByLabelText(/display name/i)).toHaveValue('Second File')
  })
})
