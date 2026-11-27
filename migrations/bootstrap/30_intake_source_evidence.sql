-- Source policies, mappings, deliveries, receipts, and scheduled pulls.

CREATE TABLE public.provider_source_settings (
    provider_id uuid NOT NULL,
    settings jsonb NOT NULL,
    version integer NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_source_settings_settings_check CHECK ((jsonb_typeof(settings) = 'object'::text)),
    CONSTRAINT provider_source_settings_version_check CHECK ((version > 0))
);

CREATE TABLE public.scheduled_source_pulls (
    provider text NOT NULL,
    source_key text NOT NULL,
    profile_sha256 text NOT NULL,
    cursor text,
    visited_cursors text[] DEFAULT '{}'::text[] NOT NULL,
    next_run_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_id uuid,
    lease_expires_at timestamp with time zone,
    failure_count integer DEFAULT 0 NOT NULL,
    last_fetch_at timestamp with time zone,
    last_payload_sha256 text,
    last_error text,
    last_page_policy_conflicts integer DEFAULT 0 NOT NULL,
    CONSTRAINT scheduled_source_pulls_last_page_policy_conflicts_check CHECK ((last_page_policy_conflicts >= 0)),
    CONSTRAINT scheduled_source_pulls_profile_sha256_check CHECK ((profile_sha256 ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE public.source_intake_receipts (
    id uuid NOT NULL,
    instance_id uuid NOT NULL,
    source_kind text NOT NULL,
    source_id text NOT NULL,
    idempotency_key text NOT NULL,
    external_id text NOT NULL,
    raw_payload jsonb NOT NULL,
    raw_sha256 text NOT NULL,
    normalized_document jsonb NOT NULL,
    mapping_version_id uuid,
    document_id uuid NOT NULL,
    job_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    replay_evidence_key text,
    replay_evidence_payload jsonb,
    replay_evidence_sha256 text,
    replay_evidence_observed_at timestamp with time zone,
    CONSTRAINT source_intake_receipts_normalized_document_check CHECK ((jsonb_typeof(normalized_document) = 'object'::text)),
    CONSTRAINT source_intake_receipts_raw_sha256_check CHECK ((raw_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT source_intake_receipts_replay_evidence_check CHECK (((num_nonnulls(replay_evidence_key, replay_evidence_payload, replay_evidence_sha256, replay_evidence_observed_at) = 0) OR ((source_kind = 'crawl'::text) AND (num_nonnulls(replay_evidence_key, replay_evidence_payload, replay_evidence_sha256, replay_evidence_observed_at) = 4) AND (replay_evidence_key ~ '^[0-9a-f]{64}$'::text) AND (replay_evidence_sha256 ~ '^[0-9a-f]{64}$'::text)))),
    CONSTRAINT source_intake_receipts_source_kind_check CHECK ((source_kind = ANY (ARRAY['webhook'::text, 'crawl'::text])))
);

CREATE TABLE public.webhook_deliveries (
    source_id uuid NOT NULL,
    idempotency_key text NOT NULL,
    body_sha256 text NOT NULL,
    raw_payload jsonb NOT NULL,
    raw_storage_uri text,
    mapping_version_id uuid NOT NULL,
    receipt_ids uuid[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT webhook_deliveries_body_sha256_check CHECK ((body_sha256 ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE public.webhook_mapping_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id uuid NOT NULL,
    version integer NOT NULL,
    mapping jsonb NOT NULL,
    mapping_sha256 text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT webhook_mapping_versions_mapping_check CHECK ((jsonb_typeof(mapping) = 'object'::text)),
    CONSTRAINT webhook_mapping_versions_mapping_sha256_check CHECK ((mapping_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT webhook_mapping_versions_version_check CHECK ((version > 0))
);

CREATE TABLE public.webhook_sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    instance_id uuid NOT NULL,
    source_key text NOT NULL,
    service_client text NOT NULL,
    service_subject text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    auto_release boolean DEFAULT false NOT NULL,
    mapping_version integer NOT NULL,
    release_network_id text,
    CONSTRAINT webhook_sources_mapping_version_check CHECK ((mapping_version > 0)),
    CONSTRAINT webhook_sources_release_network_check CHECK (((release_network_id IS NULL) OR (auto_release AND (release_network_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$'::text))))
);

ALTER TABLE ONLY public.provider_source_settings
    ADD CONSTRAINT provider_source_settings_pkey PRIMARY KEY (provider_id);

ALTER TABLE ONLY public.scheduled_source_pulls
    ADD CONSTRAINT scheduled_source_pulls_pkey PRIMARY KEY (provider, source_key, profile_sha256);

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_instance_id_source_kind_source_id_id_key UNIQUE (instance_id, source_kind, source_id, idempotency_key);

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_job_id_key UNIQUE (job_id);

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY (source_id, idempotency_key);

ALTER TABLE ONLY public.webhook_mapping_versions
    ADD CONSTRAINT webhook_mapping_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.webhook_mapping_versions
    ADD CONSTRAINT webhook_mapping_versions_source_id_version_key UNIQUE (source_id, version);

ALTER TABLE ONLY public.webhook_sources
    ADD CONSTRAINT webhook_sources_instance_id_source_key_key UNIQUE (instance_id, source_key);

ALTER TABLE ONLY public.webhook_sources
    ADD CONSTRAINT webhook_sources_pkey PRIMARY KEY (id);

CREATE INDEX source_intake_document_history ON public.source_intake_receipts USING btree (instance_id, source_kind, source_id, external_id, created_at DESC, id DESC);

CREATE UNIQUE INDEX source_intake_receipts_replay_evidence_key ON public.source_intake_receipts USING btree (instance_id, source_id, replay_evidence_key) WHERE (replay_evidence_key IS NOT NULL);

ALTER TABLE ONLY public.provider_source_settings
    ADD CONSTRAINT provider_source_settings_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.instances(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.workflows(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.source_intake_receipts
    ADD CONSTRAINT source_intake_receipts_mapping_version_id_fkey FOREIGN KEY (mapping_version_id) REFERENCES public.webhook_mapping_versions(id);

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_mapping_version_id_fkey FOREIGN KEY (mapping_version_id) REFERENCES public.webhook_mapping_versions(id);

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.webhook_sources(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.webhook_mapping_versions
    ADD CONSTRAINT webhook_mapping_versions_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.webhook_sources(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.webhook_sources
    ADD CONSTRAINT webhook_sources_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id) ON DELETE CASCADE;
