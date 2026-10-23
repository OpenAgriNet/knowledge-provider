---
status: accepted
---

# Publish to Network under a per-Group provider

Context: ADR 0003 announced every document under one provider, with one catalog per Knowledge Kind. A state's experience (for example Maharashtra's, through DSS) needs to consume only its own state's knowledge from the network, and one shared provider gives it no way to do that (engineering-tracker#190).

Decision:
- Every catalog has a provider per Knowledge Kind and instance. By default its id and descriptor code are `{kind}.{instance}.provider.oan.dev` (`advisory.mh.provider.oan.dev`, `scheme.bv.provider.oan.dev`). It is named `Advisories|Schemes from documents`, prefixed with the Group name for a Group (`Maharashtra Advisories from documents`).
- Documents with a Group use the group code as the instance. Catalog and resource ids are today's ids prefixed with the group code (`mh-cat-oan-knowledge-provider-advisories`, `mh-res-oan-knowledge-provider-advisories`, and the same for schemes), and the catalog takes the provider's name.
- The group code is derived from the document's `instance`, not from the uploader's group, so a Super Admin uploading for a state publishes under that state.
- Documents with no Group (Bharat Vistaar `bv`, `default`) use the portal instance `bv` in the provider id and keep ADR 0003's unprefixed catalog and resource ids.
- Every Group publishes; only an unmapped Knowledge Kind skips. A real publish failure stays fatal, per ADR 0004.
- The provider id template, the per-kind provider names and the Group name template are environment settings, so a deployment can change them without a code change. A template with an unknown placeholder, an id template missing `{kind}` or `{instance}`, a Group name template missing `{group_name}`, or a provider id (rendered or overridden) outside lowercase letters, digits, `-` and dot-separated parts stops the API and the worker from booting, since a published id can't be removed later. A document whose instance code still renders an invalid id fails its publish, without retry, before anything is sent.
- An instance that wants its own provider id overrides it per Knowledge Kind in a JSON file whose path is `PROVIDER_CONFIG_OVERRIDE_PATH`, e.g. `{"ka": {"advisory": "advisory.ka.com", "scheme": "scheme.ka.com"}}`. Instances and kinds it leaves out keep the template id. It defaults to a bundled empty `pipeline/network_group_providers.json`, so onboarding a Group needs no config. A missing or malformed file stops both the API and the worker from booting, so a bad deploy can't quietly publish under the wrong id.
- "No Group" follows the configured `PORTAL_INSTANCE` and `DEFAULT_INSTANCE` values rather than hardcoding `bv` and `default`.
- The publish endpoint stays the same for every Group.

Consequences:
- Catalog and resource ids are permanent under `MERGE` (ADR 0003). State knowledge published before this change stays in the unprefixed catalogs; nothing moves it to the Group's `mh-` catalogs.
- The next `bv` publish re-announces the unprefixed catalogs under `advisory.bv`/`scheme.bv` instead of `knowledge-provider`, keeping their catalog and resource ids. Changing the id template or an override later does the same. Whether the network moves such a catalog to the new provider or keeps the old entry depends on the adapter, and is untested.
- Depends on the network adapter layer accepting the new provider ids, for `bv` as well as Groups, and the Groups' prefixed catalog and resource ids.
- Chunks gain no `group` field; they keep `instance` and `instance_name`, where Bharat Vistaar and `default` documents carry `bv`/`default` and state documents carry the state code, so #202 can filter on `instance`.
- Chunks of advisory and scheme documents carry `provider_id`, the same provider id their document publishes under, and search can be limited to one or more provider ids (engineering-tracker#219). It is worked out by the same code as the publish, so the two can't drift. Other kinds carry none. The field is snake_case like every other chunk field. Chunks indexed before it existed get it from `scripts/backfill_provider_id.py`, and a document whose kind changes after ingest keeps the old value until it is reingested or promoted.
