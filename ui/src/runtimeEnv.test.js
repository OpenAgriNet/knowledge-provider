import { describe, expect, it } from 'vitest'
import { readEnv } from './runtimeEnv'

describe('readEnv', () => {
  const build = { VITE_API_BASE: '/build-api' }

  it('prefers the runtime value', () => {
    expect(readEnv('API_BASE', { API_BASE: '/runtime-api' }, build)).toBe('/runtime-api')
  })

  it('falls back to the build value when runtime is empty', () => {
    expect(readEnv('API_BASE', { API_BASE: '' }, build)).toBe('/build-api')
  })

  it('falls back when the runtime value is an unrendered placeholder', () => {
    expect(readEnv('API_BASE', { API_BASE: '${API_BASE}' }, build)).toBe('/build-api')
  })

  it('falls back when no runtime config is loaded', () => {
    expect(readEnv('API_BASE', undefined, build)).toBe('/build-api')
  })

  it('returns undefined when neither is set', () => {
    expect(readEnv('KEYCLOAK_URL', {}, {})).toBeUndefined()
  })
})
