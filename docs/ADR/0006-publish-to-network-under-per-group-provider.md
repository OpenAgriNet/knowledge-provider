---
status: accepted
---

# Publish to Network under a per-Group provider

Context: ADR 0003 announced every document under one provider, with one catalog per Knowledge Kind. A state's experience (for example Maharashtra's, through DSS) needs to consume only its own state's knowledge from the network, and one shared provider gives it no way to do that (engineering-tracker#190).

Decision:
- Every catalog has a provider per Knowledge Kind and instance. Its id and descriptor code are `{kind}.{instance}.provider.oan.dev` (`advisory.mh.provider.oan.dev`, `scheme.bv.provider.oan.dev`). It is named `Advisories|Schemes from documents`, prefixed with the Group name for a Group (`Maharashtra Advisories from documents`).
- Documents with a Group use the group code as the instance. Catalog and resource ids are today's ids prefixed with the group code (`mh-cat-oan-knowledge-provider-advisories`, `mh-res-oan-knowledge-provider-advisories`, and the same for schemes), and the catalog takes the provider's name.
- The group code is derived from the document's `instance`, not from the uploader's group, so a Super Admin uploading for a state publishes under that state.
- Documents with no Group (Bharat Vistaar `bv`, `default`) use the portal instance `bv` in the provider id and keep ADR 0003's unprefixed catalog and resource ids.
- A Group not listed in that file skips the publish with a warning, like an unmapped Knowledge Kind. It is not a failure. A real publish failure stays fatal, per ADR 0004.
- The provider id template, the per-kind provider names and the Group name template are environment settings, so a deployment can change them without a code change. A template with an unknown placeholder stops the API and the worker from booting.
- The Groups that publish are listed in a JSON array whose path is `NETWORK_GROUP_PROVIDERS_CONFIG_PATH` (codes case-insensitive), defaulting to a bundled empty `pipeline/network_group_providers.json`. Every id is derived, so onboarding a Group is one entry in that file. A missing or malformed file stops both the API and the worker from booting, so a bad deploy can't quietly skip every Group.
- A Group skip is recorded with `reason_code: group_provider_not_configured` and shown as a warning on the document page, but only while it is the document's latest publish attempt. A Knowledge Kind skip shows nothing.
- "No Group" follows the configured `PORTAL_INSTANCE` and `DEFAULT_INSTANCE` values rather than hardcoding `bv` and `default`.
- The publish endpoint stays the same for every Group.

Consequences:
- Ids are permanent under `MERGE` (ADR 0003). Documents already published under the old shared provider `knowledge-provider` are not moved, so state knowledge published before this change stays there; the next `bv` publish re-announces the unprefixed catalogs under `advisory.bv`/`scheme.bv`. Changing the id template later strands entries the same way.
- Depends on the network adapter layer accepting the per-Group provider, catalog and resource ids.
- Only the Publish to Network payload changes. Chunks and the search indexes gain no `group` field; they keep `instance` and `instance_name`, where Bharat Vistaar and `default` documents carry `bv`/`default` and state documents carry the state code, so #202 can filter on `instance`.
