-- Workflow history, artifacts, and the revision-to-workflow foreign key.

CREATE TABLE public.workflows (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    workflow_type text NOT NULL,
    temporal_workflow_id text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    current_stage text DEFAULT 'registered'::text NOT NULL,
    stage_timestamps jsonb DEFAULT '{}'::jsonb NOT NULL,
    stop_after_ocr boolean DEFAULT false NOT NULL,
    error_message text,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    started_by_user_id uuid,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflows_config_check CHECK ((jsonb_typeof(config) = 'object'::text)),
    CONSTRAINT workflows_stage_timestamps_check CHECK (((jsonb_typeof(stage_timestamps) = 'object'::text) AND ((stage_timestamps - ARRAY['ocr'::text, 'translation'::text, 'chunks'::text]) = '{}'::jsonb)))
);

CREATE VIEW public.document_current_workflow AS
 SELECT DISTINCT ON (workflows.document_id) workflows.id,
    workflows.document_id,
    workflows.workflow_type,
    workflows.temporal_workflow_id,
    workflows.status,
    workflows.current_stage,
    workflows.stage_timestamps,
    workflows.stop_after_ocr,
    workflows.error_message,
    workflows.config,
    workflows.started_by_user_id,
    workflows.started_at,
    workflows.completed_at,
    workflows.updated_at
   FROM public.workflows
  ORDER BY workflows.document_id, workflows.started_at DESC, workflows.id DESC;

CREATE TABLE public.workflow_artifacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_id uuid NOT NULL,
    artifact_type text NOT NULL,
    stage text,
    storage_uri text NOT NULL,
    mime_type text,
    filename text,
    size_bytes bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_artifacts_artifact_type_check CHECK ((btrim(artifact_type) <> ''::text)),
    CONSTRAINT workflow_artifacts_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text)),
    CONSTRAINT workflow_artifacts_size_bytes_check CHECK ((size_bytes >= 0))
);

ALTER TABLE ONLY public.workflow_artifacts
    ADD CONSTRAINT workflow_artifacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.workflows
    ADD CONSTRAINT workflows_pkey PRIMARY KEY (id);

CREATE INDEX workflow_artifacts_type_idx ON public.workflow_artifacts USING btree (workflow_id, artifact_type, created_at DESC);

CREATE INDEX workflow_artifacts_workflow_idx ON public.workflow_artifacts USING btree (workflow_id, created_at DESC);

CREATE INDEX workflows_document_started_idx ON public.workflows USING btree (document_id, started_at DESC);

CREATE INDEX workflows_status_started_idx ON public.workflows USING btree (status, started_at DESC);

CREATE INDEX workflows_temporal_id_idx ON public.workflows USING btree (temporal_workflow_id);

CREATE TRIGGER trg_workflows_updated_at BEFORE UPDATE ON public.workflows FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE ONLY public.document_revisions
    ADD CONSTRAINT document_revisions_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.workflows(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workflow_artifacts
    ADD CONSTRAINT workflow_artifacts_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.workflows(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workflows
    ADD CONSTRAINT workflows_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workflows
    ADD CONSTRAINT workflows_started_by_user_id_fkey FOREIGN KEY (started_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
