-- Search settings, audit history, and catalog interfaces.

CREATE TABLE public.audit_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid,
    workflow_id text,
    instance_id uuid,
    action_type text NOT NULL,
    entity_type text,
    entity_number integer,
    field_name text,
    old_value text,
    new_value text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    actor_user_id uuid,
    actor jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT audit_logs_action_type_check CHECK ((btrim(action_type) <> ''::text)),
    CONSTRAINT audit_logs_actor_check CHECK ((jsonb_typeof(actor) = 'object'::text)),
    CONSTRAINT audit_logs_check CHECK (((entity_type = ANY (ARRAY['setting'::text, 'catalog'::text])) OR (workflow_id IS NOT NULL))),
    CONSTRAINT audit_logs_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text))
);

CREATE TABLE public.catalogs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    kind text DEFAULT 'document'::text NOT NULL,
    type text NOT NULL,
    name text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT catalogs_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text)),
    CONSTRAINT catalogs_type_check CHECK ((type ~ '^[a-z0-9]+:[A-Za-z0-9]+$'::text))
);

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

CREATE TABLE public.search_settings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    singleton boolean DEFAULT true NOT NULL,
    retrieval jsonb DEFAULT '{}'::jsonb NOT NULL,
    candidates jsonb DEFAULT '{}'::jsonb NOT NULL,
    results jsonb DEFAULT '{}'::jsonb NOT NULL,
    query jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT search_settings_candidates_check CHECK ((jsonb_typeof(candidates) = 'object'::text)),
    CONSTRAINT search_settings_query_check CHECK ((jsonb_typeof(query) = 'object'::text)),
    CONSTRAINT search_settings_results_check CHECK ((jsonb_typeof(results) = 'object'::text)),
    CONSTRAINT search_settings_retrieval_check CHECK ((jsonb_typeof(retrieval) = 'object'::text)),
    CONSTRAINT search_settings_singleton_check CHECK (singleton)
);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.catalogs
    ADD CONSTRAINT catalogs_instance_id_kind_key UNIQUE (instance_id, kind);

ALTER TABLE ONLY public.catalogs
    ADD CONSTRAINT catalogs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.search_settings
    ADD CONSTRAINT search_settings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.search_settings
    ADD CONSTRAINT search_settings_singleton_key UNIQUE (singleton);

CREATE INDEX audit_logs_action_idx ON public.audit_logs USING btree (action_type, created_at DESC);

CREATE INDEX audit_logs_created_idx ON public.audit_logs USING btree (created_at DESC);

CREATE INDEX audit_logs_document_idx ON public.audit_logs USING btree (document_id, created_at DESC);

CREATE INDEX audit_logs_instance_idx ON public.audit_logs USING btree (instance_id, created_at DESC);

CREATE INDEX audit_logs_settings_idx ON public.audit_logs USING btree (created_at DESC) WHERE (entity_type = 'setting'::text);

CREATE TRIGGER trg_catalogs_updated_at BEFORE UPDATE ON public.catalogs FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_search_settings_updated_at BEFORE UPDATE ON public.search_settings FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.catalogs
    ADD CONSTRAINT catalogs_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id) ON DELETE RESTRICT;

INSERT INTO public.search_settings DEFAULT VALUES;
