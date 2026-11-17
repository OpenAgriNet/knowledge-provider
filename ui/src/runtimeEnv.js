const PLACEHOLDER = /^\$\{[A-Z0-9_]+\}$/

function runtimeValue(runtime, name) {
  const value = runtime?.[name]
  if (value === undefined || value === null) return undefined
  const text = String(value)
  if (text === '' || PLACEHOLDER.test(text)) return undefined
  return text
}

export function readEnv(name, runtime, build) {
  return runtimeValue(runtime, name) ?? build?.[`VITE_${name}`]
}

export function getEnv(name) {
  const runtime = typeof window === 'undefined' ? undefined : window.__ENV__
  return readEnv(name, runtime, import.meta.env)
}
