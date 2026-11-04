import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NewDocumentView from './NewDocumentView'

const apiFetch = vi.fn()

vi.mock('../auth/keycloak', () => ({ apiFetch: (...args) => apiFetch(...args) }))
vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({
    hasPermission: () => true,
    instances: ['mh'],
    isSuperAdmin: false,
    user: {},
  }),
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

describe('NewDocumentView display name', () => {
  beforeEach(() => {
    apiFetch.mockReset()
    apiFetch.mockResolvedValue({ ok: true, json: async () => ({ workflow_id: 'wf-1' }) })
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
    const field = screen.getByLabelText(/display name/i)
    await user.clear(field)
    await user.type(field, 'Cotton Pest Advisory')
    await user.click(screen.getByRole('button', { name: /start ingestion/i }))

    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    const body = apiFetch.mock.calls[0][1].body
    expect(body.get('display_name')).toBe('Cotton Pest Advisory')
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
