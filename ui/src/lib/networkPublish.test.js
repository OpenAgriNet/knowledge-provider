import { describe, expect, it } from 'vitest'
import { groupProviderSkipReason } from './networkPublish'

const artifact = (id, artifactType, metadata) => ({
  id,
  artifact_type: artifactType,
  stage: 'publishing_to_network',
  metadata_json: metadata ? JSON.stringify(metadata) : null,
})

const GROUP_SKIP = {
  skipped: true,
  reason_code: 'group_provider_not_configured',
  group: 'mh',
  reason: 'Not published: no network provider is configured for Maharashtra (mh)',
}

describe('groupProviderSkipReason', () => {
  it('returns the reason when the latest publish skipped for a missing Group Provider', () => {
    expect(groupProviderSkipReason([artifact(3, 'network_publish_skipped', GROUP_SKIP)])).toBe(GROUP_SKIP.reason)
  })

  it('ignores a skip for an unmapped knowledge kind', () => {
    const kindSkip = { skipped: true, reason_code: 'unmapped_kind', reason: 'no network catalog is mapped' }
    expect(groupProviderSkipReason([artifact(3, 'network_publish_skipped', kindSkip)])).toBeNull()
  })

  it('clears once a later publish went through', () => {
    const artifacts = [
      artifact(2, 'network_publish_skipped', GROUP_SKIP),
      artifact(5, 'network_publish_payload', { result_status: 'ACCEPTED' }),
    ]
    expect(groupProviderSkipReason(artifacts)).toBeNull()
  })

  it('ignores artifacts from other stages', () => {
    const other = { ...artifact(9, 'chunk_export', { ok: true }), stage: 'chunking' }
    expect(groupProviderSkipReason([artifact(3, 'network_publish_skipped', GROUP_SKIP), other])).toBe(GROUP_SKIP.reason)
  })

  it('tolerates missing or unreadable metadata', () => {
    expect(groupProviderSkipReason(undefined)).toBeNull()
    expect(groupProviderSkipReason([])).toBeNull()
    expect(groupProviderSkipReason([{ ...artifact(1, 'network_publish_skipped'), metadata_json: '{bad' }])).toBeNull()
  })
})
