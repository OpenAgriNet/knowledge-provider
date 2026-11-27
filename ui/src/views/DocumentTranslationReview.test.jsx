import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { fetchJson } from '../lib/pipelineUi'
import DocumentOpsView from './DocumentOpsView'

vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ hasPermissionForInstance: (permission) => permission === 'review' }),
}))
vi.mock('../components/SourcePdfPreview', () => ({ default: () => null }))
vi.mock('../components/PagePager', () => ({ default: () => null }))
vi.mock('../lib/pipelineUi', async (importOriginal) => ({
  ...(await importOriginal()),
  fetchJson: vi.fn(),
}))

it('keeps translation drafts separate and saves the selected language', async () => {
  const root = '/documents/wf-1'
  const page = {
    page_number: 1,
    original_markdown: 'Original text',
    detected_language: 'es',
    translation_target_language: 'en',
    translations: {
      en: { translated_markdown: 'English copy' },
      'fr-ca': { translated_markdown: 'Version française' },
    },
  }
  fetchJson.mockImplementation(async (path, options) => {
    if (path === root) {
      return { workflow_id: 'wf-1', filename: 'notice.txt', name: 'Notice', instance: 'demo', stage: 'completed' }
    }
    if (path === `${root}/pages`) return [page]
    if (path === `${root}/pages/1` && options?.method === 'PATCH') {
      const body = JSON.parse(options.body)
      page.translations[body.translation_language].edited_translation = body.edited_translation
      return page
    }
    return []
  })

  const user = userEvent.setup()
  render(
    <MemoryRouter initialEntries={[root]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Routes><Route path="/documents/:workflowId" element={<DocumentOpsView />} /></Routes>
    </MemoryRouter>,
  )
  await user.click(await screen.findByRole('tab', { name: 'Translation' }))
  const languages = await screen.findByRole('combobox', { name: 'Translation language' })
  const text = screen.getByRole('textbox', { name: 'Translated text' })
  expect(text).toHaveValue('English copy')
  await user.type(text, ' draft')
  await user.selectOptions(languages, 'fr-ca')
  expect(text).toHaveValue('Version française')
  await user.type(text, ' corrigée')
  await user.click(screen.getByRole('button', { name: /^save$/i }))

  await waitFor(() => {
    const [, request] = fetchJson.mock.calls.find(([path, options]) => path === `${root}/pages/1` && options?.method === 'PATCH')
    expect(JSON.parse(request.body)).toEqual({
      translation_language: 'fr-ca', edited_translation: 'Version française corrigée', translation_reviewed: true,
    })
  })
  await user.selectOptions(languages, 'en')
  expect(text).toHaveValue('English copy draft')
})
