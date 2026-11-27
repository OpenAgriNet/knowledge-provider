/** Display a provider code without interpreting it as geography. */
export function formatInstanceLabel(instance) {
  const code = String(instance || '')
    .trim()
    .toLowerCase()
  if (!code || code === 'default') return null
  return code.toUpperCase()
}

export function instanceBadgeTitle(instance) {
  const code = String(instance || '')
    .trim()
    .toLowerCase()
  if (!code || code === 'default') return 'Default instance'
  return `Instance ${code.toUpperCase()}`
}
