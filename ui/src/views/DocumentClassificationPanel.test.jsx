import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchJson } from '../lib/pipelineUi'
import { DocumentClassificationPanel } from './DocumentOpsView'

vi.mock('../lib/pipelineUi', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchJson: vi.fn(),
}))

const baseDoc = { kind: 'advisory', filename: 'scan_0042.pdf', name: 'Cotton Pest Advisory' }

function renderPanel(doc = baseDoc, props = {}) {
  const onSaved = vi.fn().mockResolvedValue()
  render(
    <DocumentClassificationPanel doc={doc} workflowId="wf-1" canClassify onSaved={onSaved} {...props} />,
  )
  return { onSaved }
}

function patchBody() {
  return JSON.parse(fetchJson.mock.calls[0][1].body)
}

describe('DocumentClassificationPanel display name', () => {
  beforeEach(() => {
    fetchJson.mockReset()
    fetchJson.mockResolvedValue({})
  })

  it('shows the stored display name', () => {
    renderPanel()
    expect(screen.getByLabelText(/display name/i)).toHaveValue('Cotton Pest Advisory')
  })

  it('falls back to the filename without its extension', () => {
    renderPanel({ ...baseDoc, name: null })
    expect(screen.getByLabelText(/display name/i)).toHaveValue('scan_0042')
  })

  it('is already saved when nothing changed', () => {
    renderPanel()
    expect(screen.getByRole('button', { name: /saved/i })).toBeDisabled()
  })

  it('sends the edited display name', async () => {
    const user = userEvent.setup()
    const { onSaved } = renderPanel()
    const field = screen.getByLabelText(/display name/i)
    await user.clear(field)
    await user.type(field, ' Bollworm Control ')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(patchBody()).toMatchObject({ name: 'Bollworm Control' })
    // Document type is read-only after upload, so it is never sent back.
    expect(patchBody()).not.toHaveProperty('kind')
  })

  it('blocks saving a blank display name', async () => {
    const user = userEvent.setup()
    renderPanel()
    await user.clear(screen.getByLabelText(/display name/i))

    expect(screen.getByText(/cannot be blank/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled()
  })

  it('shows the server error when saving fails', async () => {
    fetchJson.mockRejectedValue(new Error('Display name cannot change once the document has been published to Dev.'))
    const user = userEvent.setup()
    renderPanel()
    await user.type(screen.getByLabelText(/display name/i), '!')
    await user.click(screen.getByRole('button', { name: /^save$/i }))

    expect(await screen.findByText(/published to dev/i)).toBeInTheDocument()
  })

  it('is read-only without permission', () => {
    renderPanel(baseDoc, { canClassify: false })
    expect(screen.getByLabelText(/display name/i)).toBeDisabled()
  })

  it('does not offer to save a display name retyped unchanged', async () => {
    const user = userEvent.setup()
    renderPanel()
    const field = screen.getByLabelText(/display name/i)
    await user.clear(field)
    await user.type(field, 'Cotton Pest Advisory')

    expect(screen.getByRole('button', { name: /saved/i })).toBeDisabled()
    expect(fetchJson).not.toHaveBeenCalled()
  })

  it('counts a document without a stored name as already saved', () => {
    renderPanel({ ...baseDoc, name: null })
    expect(screen.getByRole('button', { name: /saved/i })).toBeDisabled()
  })

  it('locks the display name once the document has been ingested', () => {
    renderPanel({ ...baseDoc, ingested_at: '2026-11-01T10:00:00' })
    expect(screen.getByLabelText(/display name/i)).toBeDisabled()
  })
})
