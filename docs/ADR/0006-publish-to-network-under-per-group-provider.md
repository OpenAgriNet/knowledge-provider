---
status: accepted
---

# Publish to Network under a per-Group provider

Context: ADR 0003 announced every document under one provider, with one catalog per Knowledge Kind. A state's experience (for example Maharashtra's, through DSS) needs to consume only its own state's knowledge from the network, and one shared provider gives it no way to do that (engineering-tracker#190).

Decision:
- Documents with a Group publish under that Group's provider. Its id is set per deployment, for example `mh.provider.oan.dev`. Catalog and resource ids are today's ids prefixed with the group code (`mh-cat-oan-knowledge-provider-advisories`, `mh-res-oan-knowledge-provider-advisories`, and the same for schemes). Both catalog and provider are named `<Group name> - Knowledge Advisories|Schemes`.
- The group code is derived from the document's `instance`, not from the uploader's group, so a Super Admin uploading for a state publishes under that state.
- Documents with no Group (Bharat Vistaar `bv`, `default`) keep the Shared Provider and ADR 0003's unprefixed ids, unchanged.
- A Group with no provider configured skips the publish with a warning, like an unmapped Knowledge Kind. It is not a failure. A real publish failure stays fatal, per ADR 0004.
- Only the provider id is configured per Group, as one JSON map in `NETWORK_GROUP_PROVIDER_IDS` (keys case-insensitive). The catalog and resource ids are derived, so onboarding a Group is one map entry. A malformed map stops both the API and the worker from booting, so a bad deploy can't quietly skip every Group.
- The provider descriptor code is the shared one prefixed with the group code (`mh-oan-knowledge-provider`).
- A Group skip is recorded with `reason_code: group_provider_not_configured` and shown as a warning on the document page, but only while it is the document's latest publish attempt. A Knowledge Kind skip shows nothing.
- "No Group" follows the configured `PORTAL_INSTANCE` and `DEFAULT_INSTANCE` values rather than hardcoding `bv` and `default`.
- The publish endpoint stays the same for every Group.

Consequences:
- Ids are permanent under `MERGE` (ADR 0003). Documents already published under the Shared Provider are not moved, so state knowledge published before this change stays under the Shared Provider. A mistyped Group provider id leaves stranded entries on the network.
- Depends on the network adapter layer accepting the per-Group provider, catalog and resource ids.
- Chunks gain a `group` field (absent for Bharat Vistaar and `default`) alongside the unchanged `instance` and `instance_name`. Existing chunks are not backfilled, which #202's group filter must account for.
