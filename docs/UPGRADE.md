# Upgrade and restore baseline 0025

## Choose the database path

A fresh database installs the grouped bootstrap at version 0025. A SQLite backup uses the separate restore kit, which imports evidence and rebuilds a fresh Qdrant collection from Postgres. Do not mark an existing database as current without applying its schema and data changes.

The grouped bootstrap includes optional instance display names. For a database that already ran baseline 0025 before this column was added, apply this one-time SQL before deploying the API. Existing instance codes, group paths, and search indexes stay valid.

```sql
ALTER TABLE public.instances ADD COLUMN IF NOT EXISTS display_name text
    CHECK (char_length(display_name) BETWEEN 1 AND 120);
```

An existing reviewed baseline 0024 database uses [the operator upgrade](upgrades/0024_to_0025.sql). Pause API and worker writers and take a recoverable backup first. Apply the file in one transaction with the normal deployment database connection:

```sh
psql "$DATABASE_URL" --set=ON_ERROR_STOP=1 --single-transaction \
  --file docs/upgrades/0024_to_0025.sql
```

The script requires baseline 0024 and rejects later versions. It preserves catalog IDs, gives each catalog an explicit kind, uses KnowledgeAdvisory for schemes and advisories, removes the obsolete catalog mapping view, and changes index status and superseded keys to physical collection names. Old production entries retain their publication marker. It stops on missing collection names, colliding destinations or unmapped custom catalog types so an operator can resolve them without discarding state. A rollback-only PostgreSQL regression verifies the conversion.

## Rebuild search before switching

Use the matching restore kit with this checkout. It registers successful rebuilt collection ownership in Postgres. Search deliberately rejects points that have no matching successful ownership record. A private rehearsal may include unfinished documents, but never registers its collection for serving.

Pause source and operator writes during the final rebuild. Build a new collection, compare counts and provider-scoped search, then configure that collection as the local serving target before restarting API and worker. Keep the prior collection available for rollback. Test restore, collection switch and search together; a Qdrant point count alone does not prove that the API can serve the content.

## Runtime compatibility retained for replay

Temporal workflow and activity type names and old patch branches remain so existing workflow histories can replay. Public scheme and crop response aliases remain at the API boundary for current consumers. New Qdrant payloads use generic document identity, language-keyed labels and effective schema metadata. The old deployment-specific lookup profile is optional configuration. These retained contracts are separate from the removed duplicate payload fields and obsolete SQL mapping.

Network retrieval requires an explicit verified service-principal grant and a selected active revision. The local restore does not publish revisions to a network or enable NxN routing. Complete network-adapter integration remains pending.
