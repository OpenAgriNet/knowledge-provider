import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { API_BASE } from '../config'
import { appPath } from '../basePath'
import { AuthLoadingScreen } from '../components/AuthLoadingScreen'
import { normalizeProductRole, roleGrants, UserRole } from '../lib/roleCapabilities'
import {
  AUTH_ENABLED,
  applyBackendSession,
  clearPersistedSession,
  ensureFreshToken,
  getCurrentToken,
  initKeycloak,
  loadPersistedSession,
  logoutFromKeycloak,
  mergeUserWithJwtProfile,
  ROUTES,
  setCurrentToken,
  setUnauthorizedHandler,
} from './keycloak'

const AuthContext = createContext(null)
const REFRESH_INTERVAL_MS = 20000

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}

function AuthScreen({ title, message, action }) {
  return (
    <div className="flex min-h-svh items-center justify-center bg-[#f7faf8] px-5 text-[#14201b]">
      <div className="w-full max-w-md rounded-2xl border border-[#d5e0db] bg-white p-8 text-center shadow-sm">
        <h1 className="mb-3 text-xl font-semibold">{title}</h1>
        <p className="m-0 text-sm leading-relaxed text-[#5f7269]">{message}</p>
        {action ? <div className="mt-6">{action}</div> : null}
      </div>
    </div>
  )
}

function ScreenButton({ children, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border-0 bg-[#059669] px-4 py-2.5 text-sm font-semibold text-white cursor-pointer hover:bg-[#047857]"
    >
      {children}
    </button>
  )
}

async function loadBackendUser(token) {
  const res = await fetch(`${API_BASE}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!res.ok) {
    throw new Error(`auth/me failed: ${res.status}`)
  }
  return res.json()
}

function isLoginPath() {
  return typeof window !== 'undefined' && window.location.pathname === appPath(ROUTES.LOGIN)
}

/** True when localStorage already has tokens — used to avoid login flash on refresh. */
function hasSessionHint() {
  if (typeof window === 'undefined') return false
  try {
    const stored = loadPersistedSession()
    return Boolean(stored?.token)
  } catch {
    return false
  }
}

export function AuthProvider({ children }) {
  const sessionHint = hasSessionHint()
  const [isInitializing, setIsInitializing] = useState(AUTH_ENABLED)
  // If we already have tokens in storage, treat as "pending restore" not logged-out.
  const [isAuthenticated, setIsAuthenticated] = useState(!AUTH_ENABLED)
  const [user, setUser] = useState(null)
  // Keep true when a session hint exists so UI never assumes "logged out" mid-boot.
  const [bootstrapped, setBootstrapped] = useState(!AUTH_ENABLED)

  const syncUnauthenticated = useCallback((options = {}) => {
    const { clearStorage = true } = options
    setIsAuthenticated(false)
    setUser(null)
    setCurrentToken(null)
    if (clearStorage) clearPersistedSession()
  }, [])

  const applyAuthenticatedUser = useCallback(async (token) => {
    setCurrentToken(token)
    let backendUser = null
    try {
      backendUser = await loadBackendUser(token)
    } catch (err) {
      console.warn('loadBackendUser failed; using JWT profile only:', err)
    }
    const merged = mergeUserWithJwtProfile(backendUser, token)
    if (!merged) {
      throw new Error('No user profile from JWT or API')
    }
    setUser(merged)
    setIsAuthenticated(true)
    return merged
  }, [])

  useEffect(() => {
    if (!AUTH_ENABLED) {
      setIsInitializing(false)
      setIsAuthenticated(true)
      setBootstrapped(true)
      return
    }

    setUnauthorizedHandler(() => {
      syncUnauthenticated({ clearStorage: true })
      if (!isLoginPath()) {
        window.location.replace(appPath(ROUTES.LOGIN))
      }
    })

    let cancelled = false

    ;(async () => {
      try {
        // Restore the console session and renew it through the API if needed.
        const restored = await initKeycloak()
        if (cancelled) return

        const token = restored ? getCurrentToken() || loadPersistedSession()?.token : null
        if (!token) {
          setIsAuthenticated(false)
          setUser(null)
          setCurrentToken(null)
          return
        }

        setCurrentToken(token)
        await applyAuthenticatedUser(token)
      } catch (error) {
        if (cancelled) return
        console.error('Session restore failed:', error)
        // Last chance: a still-usable token in localStorage.
        const stored = loadPersistedSession()
        if (stored?.token) {
          try {
            await applyAuthenticatedUser(stored.token)
            return
          } catch (err2) {
            console.warn('Token-only restore failed:', err2)
          }
        }
        // Do not wipe storage on cancelled StrictMode races; only clear on hard failure.
        if (!cancelled) {
          syncUnauthenticated({ clearStorage: true })
        }
      } finally {
        if (!cancelled) {
          setIsInitializing(false)
          setBootstrapped(true)
        }
      }
    })()

    // Do NOT cancel in-flight restore on unmount — React StrictMode remounts
    // would otherwise race and leave auth half-cleared. The `cancelled` flag
    // only skips setState after unmount; init itself may finish and cache tokens.
    return () => {
      cancelled = true
    }
  }, [applyAuthenticatedUser, syncUnauthenticated])

  useEffect(() => {
    if (!AUTH_ENABLED || !isAuthenticated) return undefined
    const id = setInterval(() => {
      ensureFreshToken()
    }, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [isAuthenticated])

  const signInWithSession = useCallback(async (tokens) => {
    if (!applyBackendSession(tokens)) throw new Error('Sign-in did not return a session.')
    try {
      await applyAuthenticatedUser(tokens.token)
    } catch {
      syncUnauthenticated({ clearStorage: true })
      throw new Error('Could not load your account. Please sign in again.')
    }
  }, [applyAuthenticatedUser, syncUnauthenticated])

  const logout = useCallback(async () => {
    try {
      await logoutFromKeycloak()
    } catch (error) {
      console.error('Keycloak logout failed:', error)
    } finally {
      syncUnauthenticated({ clearStorage: true })
    }
  }, [syncUnauthenticated])

  const permissions = user?.permissions || []
  const roles = user?.roles || []
  const instances = user?.instances || []
  const groups = user?.groups || []
  const stateRoles = user?.state_roles || {}
  // Prefer API flag; also accept role name so profile sheet works after SSO.
  const isSuperAdmin = Boolean(
    user?.is_super_admin ||
      roles.some((r) => {
        const k = String(r || '')
          .toLowerCase()
          .replace(/-/g, '_')
        return k === 'super_admin' || k === 'superadmin'
      }),
  )
  const isGlobalViewer = Boolean(
    user?.is_global_viewer ?? user?.is_bh_viewer
      ?? roles.some((role) => normalizeProductRole(role) === UserRole.GLOBAL_VIEWER),
  )
  const displayName = user?.name || user?.username || user?.user_id || null
  const email = user?.email || null
  const primaryRole = roles[0] || null

  const hasPermission = useCallback(
    (perm) => {
      if (!AUTH_ENABLED) return true
      if (permissions.includes(perm)) return true
      // Safety net: authenticated SSO user with empty/stale permission list
      // still gets search so the sidebar is not blank after refresh.
      if (perm === 'search' && isAuthenticated) return true
      return false
    },
    [permissions, isAuthenticated],
  )
  const hasRole = useCallback(
    (role) => (!AUTH_ENABLED ? true : roles.includes(role)),
    [roles],
  )

  /** Role for a state tenant (mh, up, …). Super-admin always returns super_admin. */
  const roleForInstance = useCallback(
    (instance) => {
      if (!AUTH_ENABLED || isSuperAdmin) return 'super_admin'
      if (!instance) return null
      const key = String(instance).trim().toLowerCase()
      // A global viewer can read every provider and can hold a separate scoped role.
      return stateRoles[key] || (isGlobalViewer ? 'state_view' : null)
    },
    [isSuperAdmin, isGlobalViewer, stateRoles],
  )

  /** Whether the user may see a tenant (state). Super-admin → all. */
  const canAccessInstance = useCallback(
    (instance) => {
      if (!AUTH_ENABLED || isSuperAdmin || isGlobalViewer) return true
      if (!instance) return false
      const key = String(instance).trim().toLowerCase()
      if (instances.length === 0) return false
      return instances.map((i) => String(i).toLowerCase()).includes(key)
    },
    [isSuperAdmin, isGlobalViewer, instances],
  )

  /**
   * Permission within a state, driven by the shared role→permission table so
   * the UI cannot drift from pipeline/auth/permissions.py.
   *
   * The distinction that matters most: state_contributor holds 'review' (it
   * may approve OCR / translation / chunking) but NOT 'approve_ingestion'
   * (it may not publish to DEV).
   */
  const hasPermissionForInstance = useCallback(
    (perm, instance) => {
      if (!AUTH_ENABLED) return true
      if (isSuperAdmin) return hasPermission(perm)
      const role = normalizeProductRole(roleForInstance(instance))
      if (!role) {
        const hasScopedRoles = Object.keys(user?.state_roles || {}).length > 0
        const isMember = instances.some((id) => String(id).toLowerCase() === String(instance).toLowerCase())
        return !hasScopedRoles && isMember && hasPermission(perm)
      }
      // Platform-level capabilities are never granted by a state role.
      if (perm === 'admin' || perm === 'manage_users') return false
      return roleGrants(role, perm)
    },
    [hasPermission, instances, isSuperAdmin, roleForInstance, user?.state_roles],
  )

  const value = useMemo(
    () => ({
      authEnabled: AUTH_ENABLED,
      isAuthenticated: AUTH_ENABLED ? isAuthenticated : true,
      isInitializing,
      bootstrapped,
      sessionHint,
      user,
      username: displayName,
      displayName,
      email,
      roles,
      primaryRole,
      permissions,
      instances,
      groups,
      stateRoles,
      isSuperAdmin,
      isGlobalViewer,
      hasPermission,
      hasPermissionForInstance,
      hasRole,
      roleForInstance,
      canAccessInstance,
      signInWithSession,
      logout,
    }),
    [
      isAuthenticated,
      isInitializing,
      bootstrapped,
      sessionHint,
      user,
      displayName,
      email,
      permissions,
      roles,
      primaryRole,
      instances,
      groups,
      stateRoles,
      isSuperAdmin,
      isGlobalViewer,
      hasPermission,
      hasPermissionForInstance,
      hasRole,
      roleForInstance,
      canAccessInstance,
      signInWithSession,
      logout,
    ],
  )

  // Global bootstrap gate: never paint login/app routes until session restore finishes.
  if (AUTH_ENABLED && !bootstrapped) {
    return (
      <AuthContext.Provider value={value}>
        <AuthLoadingScreen
          title={sessionHint ? 'Welcome back…' : 'Loading…'}
          message={sessionHint ? 'Restoring your session' : 'Preparing secure sign-in'}
        />
      </AuthContext.Provider>
    )
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function NoPermissionScreen() {
  const { username, logout } = useAuth()
  return (
    <AuthScreen
      title="Access not granted"
      message={`Signed in as ${username || 'this account'}, but you have no permissions for this console. Contact an administrator.`}
      action={<ScreenButton onClick={() => void logout()}>Sign out</ScreenButton>}
    />
  )
}

/**
 * Wraps authenticated app routes. Shows a loader while restoring session;
 * only redirects to /login after bootstrap knows there is no session.
 */
export function RequireAuth({ children }) {
  const { authEnabled, isAuthenticated, isInitializing, bootstrapped } = useAuth()
  const location = useLocation()

  if (!authEnabled) return children

  if (!bootstrapped || isInitializing) {
    return (
      <AuthLoadingScreen
        title="Welcome back…"
        message="Restoring your session"
      />
    )
  }

  if (!isAuthenticated) {
    return <Navigate to={ROUTES.LOGIN} replace state={{ from: location.pathname }} />
  }

  return children
}
