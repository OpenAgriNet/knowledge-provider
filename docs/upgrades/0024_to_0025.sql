-- Operator-only upgrade from baseline 0024. Not a runtime migration.
-- Pause writers; back up the database; run this file in one transaction.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = '0024') THEN
    RAISE EXCEPTION 'Expected reviewed baseline 0024';
  END IF;
  IF EXISTS (SELECT 1 FROM schema_migrations WHERE version > '0024') THEN
    RAISE EXCEPTION 'Database already has a later baseline';
  END IF;
  IF EXISTS (SELECT 1 FROM documents d, LATERAL jsonb_each(coalesce(d.metadata->'index_status','{}')) e
             WHERE nullif(e.value->>'index_name','') IS NULL) THEN
    RAISE EXCEPTION 'Resolve missing index_name before upgrading';
  END IF;
  IF EXISTS (SELECT 1 FROM documents d, LATERAL jsonb_each(coalesce(d.metadata->'index_status','{}')) e
             GROUP BY d.id,e.value->>'index_name' HAVING count(*) > 1) THEN
    RAISE EXCEPTION 'Resolve colliding projection destinations before upgrading';
  END IF;
  IF EXISTS (SELECT 1 FROM documents d, LATERAL jsonb_each(coalesce(d.metadata->'superseded_doc_ids','{}')) e
             WHERE e.key IN ('dev','prod') AND NOT coalesce(d.metadata->'index_status','{}') ? e.key) THEN
    RAISE EXCEPTION 'Resolve superseded environment keys without a recorded collection';
  END IF;
END $$;
DROP VIEW public.catalog_status;
ALTER TABLE public.catalogs ADD COLUMN kind text;
UPDATE public.catalogs c SET kind = m.kind FROM public.legacy_document_catalog_types m WHERE c.type = m.catalog_type;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM catalogs WHERE kind IS NULL) THEN
   RAISE EXCEPTION 'Resolve custom catalog kinds before upgrading';
 END IF;
END $$;
ALTER TABLE public.catalogs DROP CONSTRAINT catalogs_instance_id_type_key;
ALTER TABLE public.catalogs ALTER COLUMN kind SET DEFAULT 'document';
ALTER TABLE public.catalogs ALTER COLUMN kind SET NOT NULL;
ALTER TABLE public.catalogs ADD CONSTRAINT catalogs_instance_id_kind_key UNIQUE(instance_id,kind);
UPDATE public.catalogs SET type='openagrinet:KnowledgeAdvisory' WHERE kind IN ('scheme','advisory');
DROP VIEW public.legacy_document_catalog_types;
UPDATE documents d SET metadata = d.metadata || jsonb_build_object(
 'index_status',coalesce((SELECT jsonb_object_agg(e.value->>'index_name',e.value || jsonb_build_object(
   'publication',coalesce((e.value->>'publication')::boolean,false) OR e.key='prod'))
   FROM jsonb_each(coalesce(d.metadata->'index_status','{}')) e),'{}'::jsonb),
 'superseded_doc_ids',coalesce((SELECT jsonb_object_agg(
   coalesce(d.metadata->'index_status'->e.key->>'index_name',e.key),e.value)
   FROM jsonb_each(coalesce(d.metadata->'superseded_doc_ids','{}')) e),'{}'::jsonb));
UPDATE search_settings SET results=jsonb_set(results,'{rerankMode}',to_jsonb(CASE results->>'rerankMode'
 WHEN 'cross-encoder' THEN 'heuristic' WHEN 'colbert' THEN 'heuristic'
 WHEN 'rrf' THEN 'rrf-lite' WHEN 'rrf_lite' THEN 'rrf-lite' ELSE results->>'rerankMode' END))
 WHERE results ? 'rerankMode';
CREATE VIEW public.catalog_status AS
 SELECT d.id AS document_id,
    c.id AS catalog_id,
    d.provider_id AS instance_id,
    g.name AS instance_name,
    COALESCE(c.type, d.kind) AS type,
    d.kind,
    d.code,
    d.name,
    d.aliases,
        CASE
            WHEN (x.is_live AND COALESCE(((d.metadata ->> 'reindex_required'::text))::boolean, false)) THEN 'pending_reindex'::text
            WHEN x.is_live THEN 'live'::text
            WHEN ((cw.current_stage = 'approval_for_prod'::text) AND (NOT d.is_disabled)) THEN 'pending_prod'::text
            ELSE 'disabled'::text
        END AS status,
    ( SELECT w.temporal_workflow_id
           FROM public.workflows w
          WHERE ((w.document_id = d.id) AND (w.workflow_type = 'pipeline'::text))
          ORDER BY w.started_at, w.id
         LIMIT 1) AS workflow_id,
    (( SELECT count(*) AS count
           FROM public.chunks ch
          WHERE (ch.document_id = d.id)))::integer AS chunk_count,
    COALESCE(((d.metadata ->> 'network_visible'::text))::boolean, true) AS network_visible,
        CASE
            WHEN x.is_live THEN (SELECT max((entry.value->>'last_indexed_at')::timestamp with time zone)
                                FROM jsonb_each(COALESCE(d.metadata->'index_status', '{}'::jsonb)) entry)
            ELSE NULL::timestamp with time zone
        END AS promoted_at
   FROM public.documents d
     LEFT JOIN public.catalogs c ON c.instance_id = d.provider_id AND c.kind = d.kind
     JOIN public.instances g ON g.id = d.provider_id
     LEFT JOIN public.document_current_workflow cw ON cw.document_id = d.id
     CROSS JOIN LATERAL ( SELECT ((cw.current_stage = 'completed'::text) AND (NOT d.is_disabled)
         AND COALESCE(((d.metadata ->> 'catalog_visible'::text))::boolean, true)
         AND NOT EXISTS (SELECT 1 FROM workflows w WHERE w.document_id = d.id AND w.config->>'release_network_id' IS NOT NULL)) AS is_live) x;

INSERT INTO schema_migrations(version,name) VALUES ('0025','reviewed_catalog_and_projection_ownership');
