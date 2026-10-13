// Publish to Network skip reasons, mirrored from
// pipeline/discovery_publish_service.py.
const GROUP_PROVIDER_NOT_CONFIGURED = 'group_provider_not_configured'

function parseMetadata(artifact) {
  try {
    return JSON.parse(artifact.metadata_json || 'null')
  } catch {
    return null
  }
}

/**
 * The warning to show when the document's latest Publish to Network was
 * skipped because its Group has no network provider configured, else null.
 *
 * Only the latest attempt counts, so the notice clears once a re-run publishes.
 * A skip for an unmapped knowledge kind is normal and never warns.
 */
export function groupProviderSkipReason(artifacts) {
  const latest = (artifacts || [])
    .filter(artifact => artifact.stage === 'publishing_to_network')
    .reduce((newest, artifact) => (!newest || artifact.id > newest.id ? artifact : newest), null)

  if (latest?.artifact_type !== 'network_publish_skipped') return null
  const metadata = parseMetadata(latest)
  if (metadata?.reason_code !== GROUP_PROVIDER_NOT_CONFIGURED) return null
  return metadata.reason || 'Not published: no network provider is configured for this group'
}
