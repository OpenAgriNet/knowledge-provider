-- Network releases, publication delivery, and Registry state.

CREATE FUNCTION public.reject_registry_operator_history_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RAISE EXCEPTION 'Registry operator history is append-only' USING ERRCODE = '55000';
END
$$;

CREATE TABLE public.network_release_staging (
    revision_id uuid NOT NULL,
    network_id text NOT NULL,
    collection text,
    provider_id text,
    expected_generation uuid,
    generation_captured boolean DEFAULT true NOT NULL,
    CONSTRAINT network_release_staging_network_id_check CHECK ((btrim(network_id) <> ''::text))
);

CREATE TABLE public.network_releases (
    document_id uuid NOT NULL,
    network_id text NOT NULL,
    revision_id uuid NOT NULL,
    transaction_id uuid NOT NULL,
    released_at timestamp with time zone DEFAULT now() NOT NULL,
    withdrawn_at timestamp with time zone,
    generation uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT network_releases_network_id_check CHECK ((btrim(network_id) <> ''::text))
);

CREATE TABLE public.publication_deliveries (
    network_id text NOT NULL,
    transaction_id uuid NOT NULL,
    document_id uuid NOT NULL,
    request_identity jsonb NOT NULL,
    receipt jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    received_at timestamp with time zone,
    CONSTRAINT publication_deliveries_network_id_check CHECK ((btrim(network_id) <> ''::text)),
    CONSTRAINT publication_deliveries_receipt_check CHECK ((jsonb_typeof(receipt) = 'object'::text)),
    CONSTRAINT publication_deliveries_request_identity_check CHECK ((jsonb_typeof(request_identity) = 'object'::text))
);

CREATE TABLE public.registry_operator_decisions (
    proposal_id uuid NOT NULL,
    outcome text NOT NULL,
    decided_by text NOT NULL,
    reason text NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT registry_operator_decisions_decided_by_check CHECK (((char_length(btrim(decided_by)) >= 1) AND (char_length(btrim(decided_by)) <= 200))),
    CONSTRAINT registry_operator_decisions_outcome_check CHECK ((outcome = ANY (ARRAY['approved'::text, 'rejected'::text]))),
    CONSTRAINT registry_operator_decisions_reason_check CHECK (((char_length(btrim(reason)) >= 1) AND (char_length(btrim(reason)) <= 1000)))
);

CREATE TABLE public.registry_operator_proposals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    network_id text NOT NULL,
    provider_id uuid NOT NULL,
    request_id uuid NOT NULL,
    capability text NOT NULL,
    version bigint NOT NULL,
    schema_pack_digest text,
    change jsonb NOT NULL,
    observed_record_id text,
    observed_revision text,
    submitted_by text NOT NULL,
    submission_reason text NOT NULL,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT registry_operator_proposals_change_check CHECK (((jsonb_typeof(change) = 'object'::text) AND (octet_length((change)::text) <= 16384))),
    CONSTRAINT registry_operator_proposals_check CHECK (((observed_record_id IS NULL) = (observed_revision IS NULL))),
    CONSTRAINT registry_operator_proposals_check1 CHECK (((capability = ''::text) = (schema_pack_digest IS NULL))),
    CONSTRAINT registry_operator_proposals_observed_record_id_check CHECK (((char_length(btrim(observed_record_id)) >= 1) AND (char_length(btrim(observed_record_id)) <= 253))),
    CONSTRAINT registry_operator_proposals_observed_revision_check CHECK (((char_length(btrim(observed_revision)) >= 1) AND (char_length(btrim(observed_revision)) <= 253))),
    CONSTRAINT registry_operator_proposals_submission_reason_check CHECK (((char_length(btrim(submission_reason)) >= 1) AND (char_length(btrim(submission_reason)) <= 1000))),
    CONSTRAINT registry_operator_proposals_submitted_by_check CHECK (((char_length(btrim(submitted_by)) >= 1) AND (char_length(btrim(submitted_by)) <= 200))),
    CONSTRAINT registry_operator_proposals_version_check CHECK ((version > 0))
);

CREATE TABLE public.registry_operator_subjects (
    network_id text NOT NULL,
    provider_id uuid NOT NULL,
    participant_id text NOT NULL,
    CONSTRAINT registry_operator_subjects_network_id_check CHECK (((char_length(btrim(network_id)) >= 1) AND (char_length(btrim(network_id)) <= 253))),
    CONSTRAINT registry_operator_subjects_participant_id_check CHECK (((char_length(btrim(participant_id)) >= 3) AND (char_length(btrim(participant_id)) <= 253)))
);

CREATE TABLE public.schema_pack_versions (
    network_id text NOT NULL,
    capability text NOT NULL,
    digest text NOT NULL,
    version text NOT NULL,
    bundle jsonb NOT NULL,
    installed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT schema_pack_versions_bundle_check CHECK ((jsonb_typeof(bundle) = 'object'::text)),
    CONSTRAINT schema_pack_versions_capability_check CHECK ((btrim(capability) <> ''::text)),
    CONSTRAINT schema_pack_versions_digest_check CHECK ((digest ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT schema_pack_versions_network_id_check CHECK ((btrim(network_id) <> ''::text)),
    CONSTRAINT schema_pack_versions_version_check CHECK ((btrim(version) <> ''::text))
);

ALTER TABLE ONLY public.network_release_staging
    ADD CONSTRAINT network_release_staging_pkey PRIMARY KEY (revision_id, network_id);

ALTER TABLE ONLY public.network_releases
    ADD CONSTRAINT network_releases_pkey PRIMARY KEY (document_id, network_id);

ALTER TABLE ONLY public.publication_deliveries
    ADD CONSTRAINT publication_deliveries_pkey PRIMARY KEY (network_id, transaction_id);

ALTER TABLE ONLY public.registry_operator_decisions
    ADD CONSTRAINT registry_operator_decisions_pkey PRIMARY KEY (proposal_id);

ALTER TABLE ONLY public.registry_operator_proposals
    ADD CONSTRAINT registry_operator_proposals_network_id_provider_id_capabili_key UNIQUE (network_id, provider_id, capability, version);

ALTER TABLE ONLY public.registry_operator_proposals
    ADD CONSTRAINT registry_operator_proposals_network_id_provider_id_request__key UNIQUE (network_id, provider_id, request_id);

ALTER TABLE ONLY public.registry_operator_proposals
    ADD CONSTRAINT registry_operator_proposals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.registry_operator_subjects
    ADD CONSTRAINT registry_operator_subjects_network_id_participant_id_key UNIQUE (network_id, participant_id);

ALTER TABLE ONLY public.registry_operator_subjects
    ADD CONSTRAINT registry_operator_subjects_pkey PRIMARY KEY (network_id, provider_id);

ALTER TABLE ONLY public.schema_pack_versions
    ADD CONSTRAINT schema_pack_versions_pkey PRIMARY KEY (network_id, capability, digest);

CREATE INDEX network_release_staging_scope_idx ON public.network_release_staging USING btree (network_id, collection);

CREATE INDEX network_releases_revision_idx ON public.network_releases USING btree (revision_id, network_id);

CREATE INDEX publication_deliveries_document_idx ON public.publication_deliveries USING btree (document_id);

CREATE TRIGGER registry_operator_decisions_append_only BEFORE DELETE OR UPDATE ON public.registry_operator_decisions FOR EACH ROW EXECUTE FUNCTION public.reject_registry_operator_history_mutation();

CREATE TRIGGER registry_operator_proposals_append_only BEFORE DELETE OR UPDATE ON public.registry_operator_proposals FOR EACH ROW EXECUTE FUNCTION public.reject_registry_operator_history_mutation();

CREATE TRIGGER registry_operator_subjects_append_only BEFORE DELETE OR UPDATE ON public.registry_operator_subjects FOR EACH ROW EXECUTE FUNCTION public.reject_registry_operator_history_mutation();

ALTER TABLE ONLY public.network_release_staging
    ADD CONSTRAINT network_release_staging_revision_id_fkey FOREIGN KEY (revision_id) REFERENCES public.document_revisions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.network_releases
    ADD CONSTRAINT network_releases_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.network_releases
    ADD CONSTRAINT network_releases_document_id_revision_id_fkey FOREIGN KEY (document_id, revision_id) REFERENCES public.document_revisions(document_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY public.publication_deliveries
    ADD CONSTRAINT publication_deliveries_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.registry_operator_decisions
    ADD CONSTRAINT registry_operator_decisions_proposal_id_fkey FOREIGN KEY (proposal_id) REFERENCES public.registry_operator_proposals(id);

ALTER TABLE ONLY public.registry_operator_proposals
    ADD CONSTRAINT registry_operator_proposals_network_id_capability_schema_p_fkey FOREIGN KEY (network_id, capability, schema_pack_digest) REFERENCES public.schema_pack_versions(network_id, capability, digest);

ALTER TABLE ONLY public.registry_operator_proposals
    ADD CONSTRAINT registry_operator_proposals_network_id_provider_id_fkey FOREIGN KEY (network_id, provider_id) REFERENCES public.registry_operator_subjects(network_id, provider_id);

ALTER TABLE ONLY public.registry_operator_subjects
    ADD CONSTRAINT registry_operator_subjects_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.instances(id) ON DELETE RESTRICT;
