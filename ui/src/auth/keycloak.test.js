import { afterEach, expect, it, vi } from 'vitest'
import { mergeUserWithJwtProfile, parseGroupsClaim, requestEmailCode, verifyEmailCode } from './keycloak'

afterEach(() => vi.unstubAllGlobals())

it('sends email and code only to the application API and keeps the retry challenge', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: true, status: 202, json: async () => ({ challenge: 'first' }) })
    .mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'That code is invalid or expired.', challenge: 'retry' }),
    })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ access_token: 'access', refresh_token: 'refresh' }) })
  vi.stubGlobal('fetch', fetch)

  const { challenge } = await requestEmailCode('reader@example.invalid')
  await expect(verifyEmailCode(challenge, '123456')).rejects.toMatchObject({ challenge: 'retry' })
  await expect(verifyEmailCode('retry', '654321')).resolves.toMatchObject({ token: 'access', refreshToken: 'refresh' })
  expect(fetch.mock.calls.map(([url, options]) => [url, options.method, JSON.parse(options.body)])).toEqual([
    ['/api/auth/email/send', 'POST', { email: 'reader@example.invalid' }],
    ['/api/auth/email/verify', 'POST', { challenge: 'first', code: '123456' }],
    ['/api/auth/email/verify', 'POST', { challenge: 'retry', code: '654321' }],
  ])
})

it.each(['/global/global-viewer', '/global/bh-viewer'])('maps %s to the global viewer role', (group) => {
  expect(parseGroupsClaim({ groups: [group, '/states/ALPHA/admin'] })).toMatchObject({
    isGlobalViewer: true,
    stateRoles: { alpha: 'state_admin' },
    roles: ['state_admin', 'global_viewer'],
  })
})

it('uses the canonical viewer flag first and accepts the legacy profile field', () => {
  const token = `ignored.${btoa(JSON.stringify({ sub: 'reader', groups: ['/global/global-viewer'] }))}.signature`

  expect(mergeUserWithJwtProfile({ is_bh_viewer: true }, token).is_global_viewer).toBe(true)
  expect(mergeUserWithJwtProfile({ is_global_viewer: false, is_bh_viewer: true }, token).is_global_viewer).toBe(false)
})
