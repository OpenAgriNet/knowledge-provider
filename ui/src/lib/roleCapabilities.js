/**
 * Product role catalog for the access / profile overlay.
 * Keep in sync with Keycloak roles + pipeline/auth/permissions.py.
 *
 * Model:
 *   Global platform roles
 *     - super_admin       — full platform access, all instances
 *     - global_viewer     — read-only, all instances
 *   State / centre (a centre is just another tenant code)
 *     - state_admin       — everything in the state
 *     - state_approver    — everything except delete
 *     - state_contributor — everything except delete and DEV publish
 *     - state_view        — view only
 */

export const UserRole = {
  SUPER_ADMIN: 'super_admin',
  GLOBAL_VIEWER: 'global_viewer',
  STATE_ADMIN: 'state_admin',
  STATE_APPROVER: 'state_approver',
  STATE_CONTRIBUTOR: 'state_contributor',
  STATE_VIEW: 'state_view',
}

/**
 * Role → permission ids, mirroring pipeline/auth/permissions.py.
 * This is the single source the UI uses to decide per-state capability.
 */
export const ROLE_PERMISSIONS = {
  [UserRole.SUPER_ADMIN]: [
    'search',
    'upload',
    'pipeline',
    'review',
    'approve_ingestion',
    'delete_own',
    'admin',
    'manage_users',
  ],
  [UserRole.GLOBAL_VIEWER]: ['search'],
  [UserRole.STATE_ADMIN]: [
    'search',
    'upload',
    'pipeline',
    'review',
    'approve_ingestion',
    'delete_own',
  ],
  [UserRole.STATE_APPROVER]: ['search', 'upload', 'pipeline', 'review', 'approve_ingestion'],
  // Keeps 'review' (OCR / translation / chunk approvals) but NOT
  // 'approve_ingestion' — a contributor cannot publish to DEV.
  [UserRole.STATE_CONTRIBUTOR]: ['search', 'upload', 'pipeline', 'review'],
  [UserRole.STATE_VIEW]: ['search'],
}

/** Does a canonical role grant a permission id? */
export function roleGrants(roleId, permission) {
  return (ROLE_PERMISSIONS[normalizeProductRole(roleId)] || []).includes(permission)
}

/** Full catalog — order used when super admin views all roles. */
export const ROLE_CATALOG = [
  {
    id: UserRole.SUPER_ADMIN,
    label: 'Super Admin',
    shortLabel: 'Platform administrator',
    scope: 'All instances',
    summary:
      'Full platform access across every instance. Manage users, settings, PROD approval, and all documents.',
    capabilities: [
      { id: 'all_states', label: 'Access all instances', allowed: true },
      { id: 'view', label: 'View all documents', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit', label: 'Edit any document', allowed: true },
      { id: 'review', label: 'Review & approve', allowed: true },
      { id: 'delete', label: 'Delete documents', allowed: true },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: true },
      { id: 'manage_users', label: 'User & role management', allowed: true },
      { id: 'prod_approve', label: 'Approve PROD promotion', allowed: true },
    ],
  },
  {
    id: UserRole.GLOBAL_VIEWER,
    label: 'Viewer',
    shortLabel: 'Read-only access to all instances',
    scope: 'All instances',
    summary:
      'Read-only access across all instances. Can browse and search all documents; cannot upload, edit, approve, or run the pipeline.',
    capabilities: [
      { id: 'all_states', label: 'Access all instances', allowed: true },
      { id: 'view', label: 'View all documents', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: false },
      { id: 'edit', label: 'Edit documents', allowed: false },
      { id: 'review', label: 'Review & approve', allowed: false },
      { id: 'delete', label: 'Delete documents', allowed: false },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: false },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
    ],
  },
  {
    id: UserRole.STATE_ADMIN,
    label: 'Instance Admin',
    shortLabel: 'Instance · full access',
    scope: 'Assigned instances',
    summary:
      'Full operational access in assigned instances: upload, edit, approve every stage, run the pipeline, and delete own documents. Cannot manage platform users or approve PROD.',
    capabilities: [
      { id: 'all_states', label: 'Access all instances', allowed: false },
      { id: 'view', label: 'View all documents in this instance', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit', label: 'Edit documents in this instance', allowed: true },
      { id: 'review', label: 'Review & approve in this instance', allowed: true },
      { id: 'approve_ingestion', label: 'Approve publish to DEV', allowed: true },
      { id: 'delete_own', label: 'Delete own documents', allowed: true },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
      { id: 'prod_approve', label: 'Approve PROD promotion', allowed: false },
    ],
  },
  {
    id: UserRole.STATE_APPROVER,
    label: 'Instance Approver',
    shortLabel: 'Instance · no delete',
    scope: 'Assigned instances',
    summary:
      'Same as Instance Admin but cannot delete documents. Uploads, edits, approves every stage including publish to DEV, and runs the pipeline.',
    capabilities: [
      { id: 'all_states', label: 'Access all instances', allowed: false },
      { id: 'view', label: 'View all documents in this instance', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit', label: 'Edit documents in this instance', allowed: true },
      { id: 'review', label: 'Review & approve in this instance', allowed: true },
      { id: 'approve_ingestion', label: 'Approve publish to DEV', allowed: true },
      { id: 'delete', label: 'Delete documents', allowed: false },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
      { id: 'prod_approve', label: 'Approve PROD promotion', allowed: false },
    ],
  },
  {
    id: UserRole.STATE_CONTRIBUTOR,
    label: 'Instance Contributor',
    shortLabel: 'Instance · no delete, no DEV publish',
    scope: 'Assigned instances',
    summary:
      'Uploads, edits, runs the pipeline, and approves the OCR, translation, and chunking gates. Cannot delete documents or approve publish to DEV.',
    capabilities: [
      { id: 'all_states', label: 'Access all instances', allowed: false },
      { id: 'view', label: 'View all documents in this instance', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: true },
      { id: 'edit', label: 'Edit documents in this instance', allowed: true },
      { id: 'review', label: 'Approve OCR, translation & chunking', allowed: true },
      { id: 'approve_ingestion', label: 'Approve publish to DEV', allowed: false },
      { id: 'delete', label: 'Delete documents', allowed: false },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: true },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
      { id: 'prod_approve', label: 'Approve PROD promotion', allowed: false },
    ],
  },
  {
    id: UserRole.STATE_VIEW,
    label: 'Instance Viewer',
    shortLabel: 'Instance · view only',
    scope: 'Assigned instances',
    summary:
      'View-only access in assigned instances. Can browse and search documents; cannot upload, edit, or run pipeline.',
    capabilities: [
      { id: 'all_states', label: 'Access all instances', allowed: false },
      { id: 'view', label: 'View documents in this instance', allowed: true },
      { id: 'search', label: 'Search in this instance', allowed: true },
      { id: 'upload', label: 'Upload documents', allowed: false },
      { id: 'edit', label: 'Edit documents', allowed: false },
      { id: 'review', label: 'Review & approve', allowed: false },
      { id: 'delete', label: 'Delete documents', allowed: false },
      { id: 'pipeline', label: 'Run pipeline / reprocess', allowed: false },
      { id: 'settings', label: 'Platform settings', allowed: false },
      { id: 'manage_users', label: 'User & role management', allowed: false },
    ],
  },
]

const PRODUCT_ROLE_IDS = new Set(ROLE_CATALOG.map((r) => r.id))

// Mirrors _ROLE_ALIASES in pipeline/auth/groups.py.
// NOTE: `contributor` maps to the WEAKEST upload role. It used to alias
// state_admin — never point it back there, or every contributor silently
// regains delete and DEV-publish rights.
const ROLE_ALIASES = {
  super_admin: UserRole.SUPER_ADMIN,
  'super-admin': UserRole.SUPER_ADMIN,
  superadmin: UserRole.SUPER_ADMIN,
  global_viewer: UserRole.GLOBAL_VIEWER,
  bh_viewer: UserRole.GLOBAL_VIEWER,
  'bh-viewer': UserRole.GLOBAL_VIEWER,
  bh_view: UserRole.GLOBAL_VIEWER,
  'bh-view': UserRole.GLOBAL_VIEWER,
  state_admin: UserRole.STATE_ADMIN,
  'state-admin': UserRole.STATE_ADMIN,
  admin: UserRole.STATE_ADMIN,
  state_approver: UserRole.STATE_APPROVER,
  'state-approver': UserRole.STATE_APPROVER,
  approver: UserRole.STATE_APPROVER,
  state_contributor: UserRole.STATE_CONTRIBUTOR,
  'state-contributor': UserRole.STATE_CONTRIBUTOR,
  contributor: UserRole.STATE_CONTRIBUTOR,
  state_view: UserRole.STATE_VIEW,
  'state-view': UserRole.STATE_VIEW,
  view: UserRole.STATE_VIEW,
}

export function normalizeProductRole(value) {
  if (!value || typeof value !== 'string') return null
  const key = value.trim().toLowerCase().replace(/\s+/g, '_').replace(/-/g, '_')
  if (ROLE_ALIASES[key]) return ROLE_ALIASES[key]
  if (ROLE_ALIASES[value.trim().toLowerCase()]) return ROLE_ALIASES[value.trim().toLowerCase()]
  if (PRODUCT_ROLE_IDS.has(key)) return key
  return null
}

/**
 * Which role cards to show in the access panel.
 * - Super admin → all three (platform catalog)
 * - Others → only product roles they actually hold
 */
export function rolesToDisplay({ isSuperAdmin, roles = [], stateRoles = {} }) {
  if (isSuperAdmin) {
    return ROLE_CATALOG
  }

  const held = new Set()
  for (const r of roles) {
    const n = normalizeProductRole(r)
    if (n) held.add(n)
  }
  for (const r of Object.values(stateRoles || {})) {
    const n = normalizeProductRole(r)
    if (n) held.add(n)
  }

  // Prefer stable catalog order
  return ROLE_CATALOG.filter((entry) => held.has(entry.id))
}

export function primaryProductRole({ isSuperAdmin, roles = [], stateRoles = {} }) {
  if (isSuperAdmin) return UserRole.SUPER_ADMIN
  const displayed = rolesToDisplay({ isSuperAdmin: false, roles, stateRoles })
  return displayed[0]?.id || null
}

export function roleLabel(roleId) {
  return ROLE_CATALOG.find((r) => r.id === roleId)?.label || roleId || 'Member'
}

/** Human state list from instances / state_roles keys. */
export function formatStateList(instances = [], stateRoles = {}) {
  const codes = new Set()
  for (const i of instances || []) {
    if (i) codes.add(String(i).toUpperCase())
  }
  for (const k of Object.keys(stateRoles || {})) {
    if (k) codes.add(String(k).toUpperCase())
  }
  return [...codes].sort()
}
