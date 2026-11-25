-- Instances, actor attribution, and sign-in throttling.

CREATE TABLE public.instances (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    keycloak_group_id text,
    name text NOT NULL,
    display_name text CHECK (char_length(display_name) BETWEEN 1 AND 120),
    path text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.email_sign_in_limits (
    address_digest bytea NOT NULL,
    available_at timestamp with time zone NOT NULL
);

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    keycloak_user_id text NOT NULL,
    username text,
    email text,
    roles text[] DEFAULT '{}'::text[] NOT NULL,
    instance_id uuid,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.email_sign_in_limits
    ADD CONSTRAINT email_sign_in_limits_pkey PRIMARY KEY (address_digest);

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_keycloak_group_id_key UNIQUE (keycloak_group_id);

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_path_key UNIQUE (path);

ALTER TABLE ONLY public.instances
    ADD CONSTRAINT instances_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_keycloak_user_id_key UNIQUE (keycloak_user_id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

CREATE INDEX email_sign_in_limits_available_at_idx ON public.email_sign_in_limits USING btree (available_at);

CREATE UNIQUE INDEX users_email_lower_key ON public.users USING btree (lower(email)) WHERE (email IS NOT NULL);

CREATE TRIGGER trg_instances_updated_at BEFORE UPDATE ON public.instances FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.instances(id) ON DELETE SET NULL;

INSERT INTO public.users (keycloak_user_id, username, roles)
VALUES ('local-admin', 'local-admin', '{superadmin}');
