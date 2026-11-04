import { describe, expect, it } from 'vitest'
import { DISPLAY_NAME_MAX_LENGTH, displayNameError, filenameStem } from './displayName'

describe('filenameStem', () => {
  it('drops the extension', () => {
    expect(filenameStem('PM Kisan Guidelines.pdf')).toBe('PM Kisan Guidelines')
  })

  it('drops only the last extension', () => {
    expect(filenameStem('report.v2.docx')).toBe('report.v2')
  })

  it('keeps a name without an extension', () => {
    expect(filenameStem('README')).toBe('README')
  })

  it('handles a missing name', () => {
    expect(filenameStem(undefined)).toBe('')
  })
})

describe('displayNameError', () => {
  it('accepts a normal name', () => {
    expect(displayNameError('Cotton Pest Advisory')).toBe('')
  })

  it('rejects blank', () => {
    expect(displayNameError('   ')).toMatch(/cannot be blank/)
  })

  it('accepts the maximum length after trimming', () => {
    expect(displayNameError(` ${'a'.repeat(DISPLAY_NAME_MAX_LENGTH)} `)).toBe('')
  })

  it('rejects over the maximum length', () => {
    expect(displayNameError('a'.repeat(DISPLAY_NAME_MAX_LENGTH + 1))).toMatch(/150/)
  })
})
