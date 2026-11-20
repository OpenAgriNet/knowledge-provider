// Keycloak authenticates the user. The API exchanges the PKCE authorization
// code and refreshes tokens using the confidential console client.

import { API_BASE } from '../config'
import { normalizeProductRole, UserRole } from '../lib/roleCapabilities'

const rawAuthEnabled = import.meta.env.VITE_AUTH_ENABLED
export const AUTH_ENABLED = String(rawAuthEnabled ?? 'false').toLowerCase() === 'false'

export const ROUTES = { LOGIN: '/login', HOME: '/' }

/** Persisted Keycloak tokens so a browser refresh keeps the session. */
const SESSION_STORAGE_KEY = 'docs-pipeline.keycloak.session'
/**
 * Marks a session whose tokens were minted by our backend against the
 * confidential client. That is now every session, SSO and OTP alike; the value
 * exists so sessions written by an older public-client build are recognised as
 * unrenewable and discarded rather than half-restored.
 */
const BACKEND_SESSION = 'backend'
const MIN_TOKEN_VALIDITY_SECONDS = 30

let currentToken = null
let unauthorizedHandler = null

export function setCurrentToken(token) {
  currentToken = token || null
}

export function getCurrentToken() {
  return currentToken
}

function isJwtExpired(token, skewSeconds = 30) {
  const claims = parseJwtPayload(token)
  if (!claims || typeof claims.exp !== 'number') return true
  return claims.exp * 1000 <= Date.now() + skewSeconds * 1000
}

export function loadPersistedSession() {
  try {
    const raw = localStorage.getItem(SESSION_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed?.token) return null
    return {
      token: typeof parsed.token === 'string' ? parsed.token : null,
      refreshToken: typeof parsed.refreshToken === 'string' ? parsed.refreshToken : null,
      idToken: typeof parsed.idToken === 'string' ? parsed.idToken : null,
      // Anything not written by the current backend-exchange flow is a session
      // from the old public-client build. Its refresh token belongs to a
      // different client, so it can never be renewed — treat it as stale.
      via: parsed.via === BACKEND_SESSION ? BACKEND_SESSION : 'legacy',
    }
  } catch {
    return null
  }
}

export function persistSession(tokens) {
  if (!tokens?.token) return
  try {
    // Both SSO and OTP tokens come from the confidential client, so only the
    // backend (which holds the secret) can renew them — keycloak-js would get
    // invalid_client either way.
    const via = tokens.via || BACKEND_SESSION
    localStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        token: tokens.token,
        refreshToken: tokens.refreshToken || null,
        idToken: tokens.idToken || null,
        via,
        savedAt: Date.now(),
      }),
    )
  } catch (err) {
    console.warn('Could not persist auth session:', err)
  }
}

export function clearPersistedSession() {
  try {
    localStorage.removeItem(SESSION_STORAGE_KEY)
  } catch {
    // ignore
  }
}

export function parseJwtPayload(token) {
  if (!token || typeof token !== 'string') return null
  const parts = token.split('.')
  if (parts.length < 2) return null
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
    return JSON.parse(atob(padded))
  } catch {
    return null
  }
}

export { UserRole }

const ROLE_RANK = {
  [UserRole.SUPER_ADMIN]: 100,
  [UserRole.STATE_ADMIN]: 60,
  [UserRole.STATE_APPROVER]: 40,
  [UserRole.STATE_CONTRIBUTOR]: 20,
  [UserRole.STATE_VIEW]: 10,
  [UserRole.GLOBAL_VIEWER]: 5,
}

/**
 * Parse Keycloak group membership paths from the JWT ``groups`` claim.
 * Paths: /global/super-admin, /states/{STATE}/{role}
 */
export function parseGroupsClaim(claims) {
  const raw = claims?.groups ?? claims?.group
  let paths = []
  if (typeof raw === 'string') {
    paths = raw
      .split(/[,;]/)
      .map((p) => p.trim())
      .filter(Boolean)
  } else if (Array.isArray(raw)) {
    paths = raw.map((p) => String(p).trim()).filter(Boolean)
  }

  let isSuperAdmin = false
  let isGlobalViewer = false
  const stateRoles = {}

  for (let path of paths) {
    if (!path.startsWith('/')) path = `/${path}`
    path = path.replace(/\/+$/, '') || '/'

    if (/^\/global\/(super[_-]?admin|superadmin)$/i.test(path)) {
      isSuperAdmin = true
      continue
    }
    if (/^\/global\/(global[_-]viewer|bh[_-]?viewer|bh[_-]?view)$/i.test(path)) {
      isGlobalViewer = true
      continue
    }

    const stateMatch = path.match(/^\/states\/([A-Za-z0-9_-]+)\/([A-Za-z0-9_-]+)$/i)
    if (!stateMatch) continue
    const state = stateMatch[1].toLowerCase()
    const role = normalizeProductRole(stateMatch[2])
    if (!state || !role || role === UserRole.SUPER_ADMIN || role === UserRole.GLOBAL_VIEWER) continue
    const current = stateRoles[state]
    if (!current || (ROLE_RANK[role] || 0) > (ROLE_RANK[current] || 0)) {
      stateRoles[state] = role
    }
  }

  const roles = new Set()
  if (isSuperAdmin) roles.add(UserRole.SUPER_ADMIN)
  if (isGlobalViewer) roles.add(UserRole.GLOBAL_VIEWER)
  Object.values(stateRoles).forEach((r) => roles.add(r))

  return {
    groups: [...new Set(paths)].sort(),
    isSuperAdmin,
    isGlobalViewer,
    stateRoles,
    // super_admin and global_viewer both span every state, so neither is scoped
    // by an instance list.
    instances: isSuperAdmin || isGlobalViewer ? [] : Object.keys(stateRoles).sort(),
    roles: [...roles].sort((a, b) => (ROLE_RANK[b] || 0) - (ROLE_RANK[a] || 0)),
  }
}

function collectRolesFromClaims(claims) {
  if (!claims || typeof claims !== 'object') return []
  const roles = new Set()

  // Prefer product roles derived from group paths
  const fromGroups = parseGroupsClaim(claims)
  for (const role of fromGroups.roles) roles.add(role)

  const realmRoles = claims.realm_access?.roles
  if (Array.isArray(realmRoles)) {
    for (const role of realmRoles) {
      const canon = normalizeProductRole(role)
      if (canon) roles.add(canon)
      else if (typeof role === 'string' && role.trim()) roles.add(role.trim())
    }
  }

  const resourceAccess = claims.resource_access
  if (resourceAccess && typeof resourceAccess === 'object') {
    for (const clientData of Object.values(resourceAccess)) {
      if (!clientData || typeof clientData !== 'object') continue
      if (Array.isArray(clientData.roles)) {
        for (const role of clientData.roles) {
          const canon = normalizeProductRole(role)
          if (canon) roles.add(canon)
          else if (typeof role === 'string' && role.trim()) roles.add(role.trim())
        }
      }
    }
  }

  if (Array.isArray(claims.roles)) {
    for (const role of claims.roles) {
      const canon = normalizeProductRole(role)
      if (canon) roles.add(canon)
      else if (typeof role === 'string' && role.trim()) roles.add(role.trim())
    }
  }

  // Drop noisy Keycloak defaults for display
  const ignore = new Set([
    'default-roles-bharat-vistaar',
    'default-roles-docs-pipeline',
    'offline_access',
    'uma_authorization',
    'account',
  ])
  return [...roles].filter((r) => !ignore.has(r) && !r.startsWith('default-roles-')).sort()
}

/**
 * Build a display profile from JWT claims (name, email, roles).
 * Used so the UI can show the real SSO identity even when AUTH_DISABLED=true
 * causes /auth/me to return the synthetic local-dev user.
 */
export function profileFromAccessToken(token) {
  const claims = parseJwtPayload(token)
  if (!claims) return null

  const email = typeof claims.email === 'string' ? claims.email.trim() : ''
  const preferred =
    typeof claims.preferred_username === 'string' ? claims.preferred_username.trim() : ''
  const fullName = typeof claims.name === 'string' ? claims.name.trim() : ''
  const given = typeof claims.given_name === 'string' ? claims.given_name.trim() : ''
  const family = typeof claims.family_name === 'string' ? claims.family_name.trim() : ''
  const composed = [given, family].filter(Boolean).join(' ').trim()

  const displayName = fullName || composed || preferred || email || ''
  const username = preferred || email || displayName || String(claims.sub || '')
  const groupAccess = parseGroupsClaim(claims)
  const roles = collectRolesFromClaims(claims)

  // Legacy multivalued instances claim when groups are absent
  let instances = groupAccess.instances
  if (!groupAccess.isSuperAdmin && instances.length === 0) {
    const raw = claims.instances ?? claims.tenants ?? claims.tenant
    if (Array.isArray(raw)) {
      instances = raw.map((i) => String(i).trim().toLowerCase()).filter(Boolean)
    } else if (typeof raw === 'string' && raw.trim()) {
      instances = raw
        .split(/[,;]/)
        .map((i) => i.trim().toLowerCase())
        .filter(Boolean)
    }
  }

  return {
    user_id: String(claims.sub || ''),
    username,
    name: displayName || username,
    email,
    roles,
    groups: groupAccess.groups,
    state_roles: groupAccess.stateRoles,
    instances,
    is_super_admin: groupAccess.isSuperAdmin,
    is_global_viewer: roles.includes(UserRole.GLOBAL_VIEWER),
    claims,
  }
}

/** Merge backend /auth/me with JWT display fields (JWT wins for identity labels). */
export function mergeUserWithJwtProfile(backendUser, token) {
  const profile = profileFromAccessToken(token)
  if (!backendUser && !profile) return null
  if (!profile) {
    return {
      ...backendUser,
      name: backendUser?.username || backendUser?.user_id || '',
    }
  }
  if (!backendUser) {
    return {
      user_id: profile.user_id,
      username: profile.username,
      name: profile.name,
      email: profile.email,
      roles: profile.roles,
      permissions: [],
      instances: profile.instances || [],
      envs: [],
      groups: profile.groups || [],
      state_roles: profile.state_roles || {},
      is_super_admin: Boolean(profile.is_super_admin),
      is_global_viewer: Boolean(profile.is_global_viewer),
      auth_disabled: false,
    }
  }

  if (backendUser.live_provider_access) {
    return { ...backendUser, name: profile.name || backendUser.username || backendUser.user_id || '' }
  }

  // Prefer JWT identity labels; keep backend permissions (incl. bypass mode).
  const jwtRoles = profile.roles || []
  const backendRoles = Array.isArray(backendUser.roles) ? backendUser.roles : []
  const displayRoles = jwtRoles.length > 0 ? jwtRoles : backendRoles

  const backendInstances = Array.isArray(backendUser.instances) ? backendUser.instances : []
  const jwtInstances = Array.isArray(profile.instances) ? profile.instances : []
  const instances =
    backendInstances.length > 0
      ? backendInstances
      : jwtInstances

  return {
    ...backendUser,
    user_id: profile.user_id || backendUser.user_id,
    username: profile.username || backendUser.username,
    name: profile.name || backendUser.username || backendUser.user_id || '',
    email: profile.email || backendUser.email || '',
    roles: displayRoles,
    instances,
    groups: backendUser.groups?.length ? backendUser.groups : profile.groups || [],
    state_roles:
      backendUser.state_roles && Object.keys(backendUser.state_roles).length > 0
        ? backendUser.state_roles
        : profile.state_roles || {},
    is_super_admin: Boolean(
      backendUser.is_super_admin ?? profile.is_super_admin,
    ),
    is_global_viewer: Boolean(backendUser.is_global_viewer ?? backendUser.is_bh_viewer ?? profile.is_global_viewer),
  }
}

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler
}

export function handleUnauthorized() {
  if (typeof unauthorizedHandler === 'function') unauthorizedHandler()
}

export function authHeaders() {
  if (!AUTH_ENABLED || !currentToken) return {}
  return { Authorization: `Bearer ${currentToken}` }
}

/**
 * @deprecated Tokens must be sent in the Authorization header only.
 * This helper is a no-op kept for any leftover call sites; it never appends a token.
 */
export function appendAccessToken(url) {
  return url
}

/**
 * Renew the session through the backend.
 *
 * Tokens are issued to a confidential Keycloak client, so a browser-side
 * refresh has no secret to present and comes back invalid_client. The backend
 * holds the secret and does the refresh_token grant on our behalf. This is the
 * only renewal path now — SSO and OTP sessions are indistinguishable here.
 *
 * @returns the new access token, or null when the session is over.
 */
async function refreshBackendSession() {
  const stored = loadPersistedSession()
  if (!stored?.refreshToken) return null
  try {
    const response = await fetch(`${API_BASE}/auth/session/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: stored.refreshToken }),
    })
    if (!response.ok) return null
    const data = await response.json()
    if (!data?.access_token) return null
    const tokens = {
      token: data.access_token,
      refreshToken: data.refresh_token || stored.refreshToken,
      idToken: data.id_token || stored.idToken,
      via: BACKEND_SESSION,
    }
    persistSession(tokens)
    setCurrentToken(tokens.token)
    return tokens.token
  } catch (err) {
    console.warn('[auth] Session refresh failed:', err)
    return null
  }
}

export async function ensureFreshToken() {
  if (!AUTH_ENABLED) return

  // Every session is backend-managed now — keycloak-js cannot refresh a
  // confidential-client token, so it is never consulted here.
  const persisted = loadPersistedSession()
  if (!persisted || persisted.via !== BACKEND_SESSION) {
    if (persisted) clearPersistedSession()
    handleUnauthorized()
    return
  }

  const active = getCurrentToken() || persisted.token
  if (active && !isJwtExpired(active, MIN_TOKEN_VALIDITY_SECONDS)) {
    setCurrentToken(active)
    return
  }
  const renewed = await refreshBackendSession()
  if (renewed) return
  clearPersistedSession()
  handleUnauthorized()
}

export async function apiFetch(url, options = {}) {
  await ensureFreshToken()
  const headers = { ...(options.headers || {}), ...authHeaders() }
  const response = await fetch(url, { ...options, headers })
  if (response.status === 401 && AUTH_ENABLED) {
    handleUnauthorized()
  }
  return response
}

/**
 * Restore a session on page load.
 *
 * There is no keycloak-js init here any more. The adapter can only drive a
 * public client: with a confidential one it cannot exchange a code, refresh a
 * token, or validate a session. Everything it used to do now happens on the
 * backend, so all this has to do is rehydrate localStorage and renew if stale.
 */
export async function initKeycloak() {
  if (!AUTH_ENABLED) return false

  const persisted = loadPersistedSession()
  if (!persisted?.token) return false

  if (persisted.via !== BACKEND_SESSION) {
    // Written by a build that talked to the old public client — its refresh
    // token is for a different client and will never be accepted again.
    clearPersistedSession()
    return false
  }

  let token = persisted.token
  if (isJwtExpired(token, MIN_TOKEN_VALIDITY_SECONDS)) {
    token = await refreshBackendSession()
  }
  if (!token) {
    clearPersistedSession()
    return false
  }

  setCurrentToken(token)
  return true
}

/**
 * Adopt tokens the backend obtained from Keycloak — SSO code exchange or OTP.
 *
 * Deliberately does not touch the Keycloak adapter: these tokens belong to a
 * confidential client, so the adapter could neither refresh nor validate them.
 * The session lives in localStorage and renews through /auth/session/refresh.
 */
export function applyBackendSession(tokens) {
  if (!tokens?.token) return false
  const session = {
    token: tokens.token,
    refreshToken: tokens.refreshToken || null,
    idToken: tokens.idToken || null,
    via: BACKEND_SESSION,
  }
  persistSession(session)
  setCurrentToken(session.token)
  return true
}

async function postAuth(path, body) {
  const response = await fetch(`${API_BASE}/auth/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (response.status === 204) return null
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const message = typeof data?.detail === 'string' ? data.detail : 'Sign-in could not be completed.'
    const error = new Error(message)
    error.challenge = data?.challenge
    throw error
  }
  return data
}

export async function requestEmailCode(email) {
  return postAuth('email/send', { email })
}

export async function verifyEmailCode(challenge, code) {
  const data = await postAuth('email/verify', { challenge, code })
  return { token: data.access_token, refreshToken: data.refresh_token, idToken: data.id_token }
}

export async function googleSignInEnabled() {
  const response = await fetch(`${API_BASE}/auth/google`)
  return response.ok && (await response.json()).enabled === true
}

export async function startGoogleSignIn() {
  const { location } = await postAuth('google/start', {})
  window.location.assign(location)
}

let googleSessionRequest

export function finishGoogleSignIn() {
  googleSessionRequest ??= postAuth('google/complete', {}).then(data => ({
    token: data.access_token, refreshToken: data.refresh_token, idToken: data.id_token,
  })).finally(() => { googleSessionRequest = undefined })
  return googleSessionRequest
}

export async function logoutFromKeycloak() {
  const session = loadPersistedSession()
  if (session?.refreshToken) {
    await postAuth('session/logout', { refresh_token: session.refreshToken })
  }
}
