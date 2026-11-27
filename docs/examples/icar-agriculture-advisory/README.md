# Apply AgricultureAdvisory to ICAR intake

The opt-in ICAR JSONata template maps each published POP entry to a Direct AgricultureAdvisory and the existing searchable advisory document. The template pins the full pack in `knowledge_metadata.schema_pack`. Knowledge Provider validates it during intake and stores the attributes and digest. The existing advisory kind, search contract, and network catalog are independent of this pin.

## Prepare the bundle

From the Knowledge Provider root, prepare the files in a new directory. Choose an approved schema registry namespace for the deployment. This namespace does not publish the document or enable network search.

```sh
uv run python -m scripts.prepare_icar_advisory_pack ./icar-pack --network-id "$ICAR_SCHEMA_NAMESPACE"
uv run python -m scripts.install_schema_pack ./icar-pack/manifest.json --install-for-network "$ICAR_SCHEMA_NAMESPACE"
```

The first command downloads the pinned public schema artifacts, verifies their checksums, validates all references offline, and writes `templates.json`. The second installs the validated pack in the deployment database. Schema validation at intake uses the installed bundle and needs no network fetch.

## Activate the mapping

Add the `icar-agriculture-advisory` entry from `templates.json` to the instance's source templates. Select it when creating a new mapping version for the ICAR webhook source. Test a crop record and a livestock record before switching the delivery source. Confirm that each receipt stores the expected digest and that indexed search still returns the advisory.

The mapping reads `issuedAt` and `language` from the POP entry, with fallback to the parent record. Before activation, confirm the production feed supplies those canonical fields or translate its documented equivalents into them. An issuance timestamp must include a timezone. A publication status, the receipt time, and season dates do not supply `issuedAt`. A record must also supply a recognized subject category, a topic, and actionable advisory text. The stable upstream source ID uses the original content and POP IDs.

Relative timing is mapped for crop sowing when ICAR supplies a valid day bound. The same condition is available to search. Document calendar validity stays in the existing validity fields. Earlier deliveries remain on their saved mapping versions, with a separate decision required for any backfill.
