-- Existing installations of the grouped bootstrap need the presentation label.
ALTER TABLE public.instances ADD COLUMN IF NOT EXISTS display_name text
    CHECK (char_length(display_name) BETWEEN 1 AND 120);
