import { getEnv } from './runtimeEnv'

export const API_BASE = (getEnv('API_BASE') || '/api').replace(/\/$/, '')

export const DOCUMENT_VALIDITY_ENABLED =
  String(getEnv('DOCUMENT_VALIDITY_ENABLED') ?? 'false').toLowerCase() === 'true'
