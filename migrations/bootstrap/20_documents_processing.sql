-- Documents, pages, chunks, and frozen revisions.

CREATE TABLE public.chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    chunk_number integer NOT NULL,
    original_text text NOT NULL,
    edited_text text,
    token_count integer DEFAULT 0 NOT NULL,
    page_start integer DEFAULT 1 NOT NULL,
    page_end integer DEFAULT 1 NOT NULL,
    source_spans jsonb DEFAULT '[]'::jsonb NOT NULL,
    section_title text,
    content_type text,
    is_reference boolean DEFAULT false NOT NULL,
    is_reviewed boolean DEFAULT false NOT NULL,
    is_excluded boolean DEFAULT false NOT NULL,
    reviewer_notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT chunks_check CHECK ((page_end >= page_start)),
    CONSTRAINT chunks_chunk_number_check CHECK ((chunk_number >= 1)),
    CONSTRAINT chunks_page_start_check CHECK ((page_start >= 1)),
    CONSTRAINT chunks_source_spans_check CHECK ((jsonb_typeof(source_spans) = 'array'::text)),
    CONSTRAINT chunks_tags_max_count CHECK ((cardinality(tags) <= 16)),
    CONSTRAINT chunks_token_count_check CHECK ((token_count >= 0))
);

CREATE TABLE public.documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    source jsonb NOT NULL,
    is_disabled boolean DEFAULT false NOT NULL,
    metadata jsonb DEFAULT '{"index_status": {}, "catalog_visible": true, "network_visible": true, "reindex_required": false}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    provider_id uuid NOT NULL,
    kind text NOT NULL,
    CONSTRAINT documents_aliases_check CHECK ((array_position(aliases, ''::text) IS NULL)),
    CONSTRAINT documents_code_check CHECK ((code ~ '^[a-z0-9]+(-[a-z0-9]+)*$'::text)),
    CONSTRAINT documents_kind_nonblank CHECK ((btrim(kind) <> ''::text)),
    CONSTRAINT documents_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text)),
    CONSTRAINT documents_source_check CHECK ((jsonb_typeof(source) = 'object'::text)),
    CONSTRAINT documents_source_check1 CHECK (((source ->> 'fingerprint'::text) ~ '^[0-9a-f]{32}$'::text))
);

CREATE TABLE public.document_revisions (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    content_sha256 text NOT NULL,
    snapshot jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT document_revisions_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT document_revisions_snapshot_check CHECK ((jsonb_typeof(snapshot) = 'object'::text))
);

CREATE TABLE public.pages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    page_number integer NOT NULL,
    original_markdown text,
    edited_markdown text,
    translations jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{"is_reviewed": false}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pages_metadata_check CHECK ((jsonb_typeof(metadata) = 'object'::text)),
    CONSTRAINT pages_page_number_check CHECK ((page_number >= 1)),
    CONSTRAINT pages_translations_check CHECK ((jsonb_typeof(translations) = 'object'::text))
);

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_chunk_number_key UNIQUE (document_id, chunk_number);

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_revisions
    ADD CONSTRAINT document_revisions_document_id_id_key UNIQUE (document_id, id);

ALTER TABLE ONLY public.document_revisions
    ADD CONSTRAINT document_revisions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_revisions
    ADD CONSTRAINT document_revisions_workflow_id_key UNIQUE (workflow_id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_provider_kind_code_key UNIQUE (provider_id, kind, code);

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_document_id_page_number_key UNIQUE (document_id, page_number);

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_pkey PRIMARY KEY (id);

CREATE INDEX chunks_text_trgm_idx ON public.chunks USING gin (lower(COALESCE(edited_text, original_text)) public.gin_trgm_ops);

CREATE INDEX document_revisions_document_idx ON public.document_revisions USING btree (document_id, created_at DESC);

CREATE INDEX documents_created_at_idx ON public.documents USING btree (created_at DESC);

CREATE INDEX documents_provider_fingerprint_idx ON public.documents USING btree (provider_id, ((source ->> 'fingerprint'::text)));

CREATE INDEX documents_provider_id_idx ON public.documents USING btree (provider_id);

CREATE TRIGGER trg_chunks_updated_at BEFORE UPDATE ON public.chunks FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_documents_updated_at BEFORE UPDATE ON public.documents FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_pages_updated_at BEFORE UPDATE ON public.pages FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.document_revisions
    ADD CONSTRAINT document_revisions_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.instances(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;
