export const DISPLAY_NAME_MAX_LENGTH = 150

export function filenameStem(name) {
  if (!name) return ''
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(0, dot) : name
}

export function displayNameError(value) {
  const text = (value || '').trim()
  if (!text) return 'Display name cannot be blank.'
  if (text.length > DISPLAY_NAME_MAX_LENGTH) {
    return `Display name must be at most ${DISPLAY_NAME_MAX_LENGTH} characters.`
  }
  return ''
}
