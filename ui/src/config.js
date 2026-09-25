// Prefer VITE_API_BASE at build time (production path prefix).
// Local Vite dev proxies /api → API container/host.
export const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')

/**
 * Document validity (start / end dates on the classification panel).
 *
 * Off by default: the backend already stamps and honours a validity period on
 * every document, but the business has not asked reviewers to set one yet.
 * The flag gates the *entry fields* only - search still filters on whatever
 * is stored, so turning it on exposes an existing capability rather than
 * switching one on.
 *
 * Build-time, matching VITE_AUTH_ENABLED: flipping it needs a UI rebuild, and
 * it is a deployment-wide decision rather than a per-user one.
 */
export const DOCUMENT_VALIDITY_ENABLED =
  String(import.meta.env.VITE_DOCUMENT_VALIDITY_ENABLED ?? 'false').toLowerCase() === 'true'
