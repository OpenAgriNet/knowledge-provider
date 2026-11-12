-- catalog_status: a document is "live" only when it is actually being served.
--
-- Before: live = the document's latest workflow was at stage 'completed'. That
-- advertised DEV-only documents as live, and hid a live document as soon as a
-- new run (reingest, retry) started.
--
-- Now: live = some workflow of the document reached 'completed' AND either the
-- PROD index holds it (metadata.index_status.prod.status = 'indexed') or it
-- finished a run that skipped PROD on purpose (workflows.config.skip_prod, set
-- from DISABLE_PROD_SETTING when the run started).
CREATE OR REPLACE VIEW catalog_status AS
SELECT
  d.id AS document_id,
  c.id AS catalog_id,
  c.instance_id,
  g.name AS instance_name,
  c.type,
  d.code,
  d.name,
  d.aliases,
  CASE
    WHEN x.is_live AND COALESCE((d.metadata->>'reindex_required')::boolean, FALSE) THEN 'pending_reindex'
    WHEN x.is_live THEN 'live'
    WHEN cw.current_stage = 'approval_for_prod' AND NOT d.is_disabled THEN 'pending_prod'
    ELSE 'disabled'
  END AS status,
  (SELECT w.temporal_workflow_id FROM workflows w
    WHERE w.document_id = d.id AND w.workflow_type = 'pipeline'
    ORDER BY w.started_at ASC, w.id ASC LIMIT 1) AS workflow_id,
  (SELECT count(*) FROM chunks ch WHERE ch.document_id = d.id)::INTEGER AS chunk_count,
  COALESCE((d.metadata->>'network_visible')::boolean, TRUE) AS network_visible,
  CASE WHEN x.is_live
       THEN (d.metadata->'index_status'->'prod'->>'last_indexed_at')::timestamptz END AS promoted_at
FROM documents d
JOIN catalogs c ON c.id = d.catalog_id
JOIN instances g ON g.id = c.instance_id
LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
CROSS JOIN LATERAL (
  SELECT (NOT d.is_disabled
          AND COALESCE((d.metadata->>'catalog_visible')::boolean, TRUE)
          AND EXISTS (SELECT 1 FROM workflows w
                      WHERE w.document_id = d.id AND w.current_stage = 'completed')
          AND (d.metadata->'index_status'->'prod'->>'status' = 'indexed'
               OR EXISTS (SELECT 1 FROM workflows w
                          WHERE w.document_id = d.id AND w.current_stage = 'completed'
                            AND w.config->>'skip_prod' = 'true'))) AS is_live
) x;
