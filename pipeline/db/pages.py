"""Persist extracted pages and their translations."""

from typing import Any, Optional

from psycopg.types.json import Jsonb

from pipeline.documents.models import (
    PageMetadata,
    PageTranslation,
)
from pipeline.metadata.localization import normalize_language_tag

from . import chunks as chunk_queries
from . import common, connection, document_reads, jobs

# =============================================================================
# Page Functions
# =============================================================================

_PAGE_SELECT = """
    SELECT p.id, p.document_id, p.page_number, p.original_markdown, p.edited_markdown,
           p.translations, p.metadata, p.created_at, p.updated_at
    FROM pages p
"""


def _primary_translation(translations: dict) -> tuple[Optional[str], dict]:
    if not translations:
        return None, {}
    lang = max(
        translations,
        key=lambda k: (translations[k].get("translated_at") or "", k == "en"),
    )
    return lang, translations[lang]


def _page_from_row(row: Optional[dict], workflow_id: str) -> Optional[dict]:
    if row is None:
        return None
    meta = row["metadata"] or {}
    translations = row["translations"] or {}
    lang, tr = _primary_translation(translations)
    page = {
        "id": row["id"],
        "workflow_id": workflow_id,
        "page_number": row["page_number"],
        "original_markdown": row["original_markdown"],
        "edited_markdown": row["edited_markdown"],
        "is_reviewed": bool(meta.get("is_reviewed")),
        "reviewer_notes": meta.get("reviewer_notes"),
        "detected_language": meta.get("detected_language"),
        "translated_markdown": tr.get("translated_markdown"),
        "edited_translation": tr.get("edited_translation"),
        "translation_reviewed": bool(tr.get("is_reviewed")),
        "translation_notes": tr.get("reviewer_notes"),
        "translation_provider": tr.get("provider"),
        "translation_model": tr.get("model"),
        "translation_target_language": lang,
        "translated_at": tr.get("translated_at"),
        "translations": translations,
        "metadata": meta,
    }
    return {k: common._clean(v) for k, v in page.items()}


_TRANSLATION_FLAT_KEYS = (
    "translated_markdown",
    "edited_translation",
    "translation_notes",
    "translation_provider",
    "translation_model",
    "translated_at",
)


def _translation_from_flat(page: dict) -> Optional[tuple[str, dict]]:
    if not any(page.get(k) not in (None, "") for k in _TRANSLATION_FLAT_KEYS) and not page.get(
        "translation_reviewed"
    ):
        return None
    lang = page.get("translation_target_language") or "en"
    entry = PageTranslation(
        translated_markdown=page.get("translated_markdown"),
        edited_translation=page.get("edited_translation"),
        is_reviewed=bool(page.get("translation_reviewed")),
        reviewer_notes=page.get("translation_notes"),
        provider=page.get("translation_provider"),
        model=page.get("translation_model"),
        translated_at=page.get("translated_at"),
    ).model_dump()
    return lang, entry


def save_pages(workflow_id: str, pages: list[dict]):
    """
    Save all pages for a document (bulk upsert).

    Each call replaces the whole row, so the caller passes every field. Page
    ``translations`` are kept as stored unless the page carries ``translations``
    (replaces them) or flat ``translated_*`` fields (merged in under
    ``translation_target_language``); a page whose OCR text changed loses its
    translations because they no longer describe it.
    """
    with connection.get_connection() as conn:
        doc_uuid = document_reads._require_document_uuid(conn, workflow_id)
        existing = {
            r["page_number"]: r
            for r in conn.execute(
                "SELECT page_number, original_markdown, translations FROM pages "
                "WHERE document_id = %s::uuid",
                (doc_uuid,),
            ).fetchall()
        }
        for page in pages:
            number = page.get("page_number")
            current = existing.get(number)
            meta = dict(page.get("metadata") or {})
            for flat_key in ("is_reviewed", "reviewer_notes", "detected_language"):
                if flat_key in page:
                    meta[flat_key] = page[flat_key]
            meta["is_reviewed"] = bool(meta.get("is_reviewed"))
            page_meta = PageMetadata.model_validate(
                {k: v for k, v in meta.items() if v is not None}
            ).model_dump(exclude_none=True)

            translations = dict(current["translations"]) if current else {}
            if current and current["original_markdown"] != page.get("original_markdown"):
                translations = {}
            if isinstance(page.get("translations"), dict):
                translations = {
                    lang: PageTranslation.model_validate(entry).model_dump()
                    for lang, entry in page["translations"].items()
                }
            else:
                flat = _translation_from_flat(page)
                if flat:
                    translations[flat[0]] = flat[1]

            conn.execute(
                """
                INSERT INTO pages (document_id, page_number, original_markdown, edited_markdown,
                                   translations, metadata)
                VALUES (%s::uuid, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, page_number) DO UPDATE SET
                    original_markdown = EXCLUDED.original_markdown,
                    edited_markdown = EXCLUDED.edited_markdown,
                    translations = EXCLUDED.translations,
                    metadata = EXCLUDED.metadata
                """,
                (
                    doc_uuid,
                    number,
                    page.get("original_markdown"),
                    page.get("edited_markdown"),
                    Jsonb(translations),
                    Jsonb(page_meta),
                ),
            )


def persist_document_content(workflow_id: str, pages: list[dict], chunks: list[dict]):
    """Backward-compatible helper to persist pages and chunks in one call."""
    save_pages(workflow_id, pages)
    chunk_queries.save_chunks(workflow_id, chunks)


def get_pages(workflow_id: str) -> list[dict]:
    """Get all pages for a document."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return []
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        rows = conn.execute(
            _PAGE_SELECT + " WHERE p.document_id = %s::uuid ORDER BY p.page_number", (doc_uuid,)
        ).fetchall()
    return [_page_from_row(r, root) for r in rows]


def count_translated_pages(workflow_id: str) -> int:
    """Count pages with translation text without loading full page bodies."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM pages p
            WHERE p.document_id = %s::uuid
              AND EXISTS (
                SELECT 1 FROM jsonb_each(p.translations) t
                WHERE nullif(btrim(t.value->>'translated_markdown'), '') IS NOT NULL
                   OR nullif(btrim(t.value->>'edited_translation'), '') IS NOT NULL
              )
            """,
            (doc_uuid,),
        ).fetchone()
    return int(row["n"] if row else 0)


def get_saved_page_numbers(workflow_id: str) -> list[int]:
    """Get saved page numbers for a document without loading full page content."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return []
        rows = conn.execute(
            "SELECT page_number FROM pages WHERE document_id = %s::uuid ORDER BY page_number",
            (doc_uuid,),
        ).fetchall()
    return [int(row["page_number"]) for row in rows]


def get_page(workflow_id: str, page_num: int) -> Optional[dict]:
    """Get a specific page."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        row = conn.execute(
            _PAGE_SELECT + " WHERE p.document_id = %s::uuid AND p.page_number = %s",
            (doc_uuid, page_num),
        ).fetchone()
    return _page_from_row(row, root)


class PageTranslationMissing(LookupError):
    """The selected translation is no longer on this page."""


def page_translation_key(translations: dict, language: str | None) -> str | None:
    normalized = normalize_language_tag(language)
    if normalized is None:
        return None
    matches = [key for key in translations if normalize_language_tag(key) == normalized]
    return matches[0] if len(matches) == 1 else None


def update_page(
    workflow_id: str,
    page_num: int,
    edited_markdown: Optional[str] = None,
    is_reviewed: Optional[bool] = None,
    reviewer_notes: Optional[str] = None,
    edited_translation: Optional[str] = None,
    translation_reviewed: Optional[bool] = None,
    translation_notes: Optional[str] = None,
    translation_language: Optional[str] = None,
) -> Optional[dict]:
    """Update a page. Returns updated page or None if not found."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        existing = conn.execute(
            "SELECT translations FROM pages WHERE document_id = %s::uuid AND page_number = %s FOR UPDATE",
            (doc_uuid, page_num),
        ).fetchone()
        if not existing:
            return None

        assignments: list[str] = []
        params: list = []
        if edited_markdown is not None:
            assignments.append("edited_markdown = %s")
            params.append(edited_markdown)

        meta_patch: dict[str, Any] = {}
        if is_reviewed is not None:
            meta_patch["is_reviewed"] = bool(is_reviewed)
        if reviewer_notes is not None:
            meta_patch["reviewer_notes"] = reviewer_notes
        if meta_patch:
            assignments.append("metadata = metadata || %s::jsonb")
            params.append(Jsonb(meta_patch))

        tr_patch: dict[str, Any] = {}
        if edited_translation is not None:
            tr_patch["edited_translation"] = edited_translation
        if translation_reviewed is not None:
            tr_patch["is_reviewed"] = bool(translation_reviewed)
        if translation_notes is not None:
            tr_patch["reviewer_notes"] = translation_notes
        if tr_patch:
            translations = existing["translations"] or {}
            if translation_language is not None:
                lang = page_translation_key(translations, translation_language)
                if lang is None:
                    raise PageTranslationMissing(translation_language)
            else:
                lang = _primary_translation(translations)[0] or "en"
            assignments.append(
                "translations = jsonb_set(translations, ARRAY[%s::text], "
                "COALESCE(translations->%s::text, '{}'::jsonb) || %s::jsonb, TRUE)"
            )
            params.extend([lang, lang, Jsonb(tr_patch)])

        if assignments:
            conn.execute(
                f"UPDATE pages SET {', '.join(assignments)} "
                "WHERE document_id = %s::uuid AND page_number = %s",
                (*params, doc_uuid, page_num),
            )
    return get_page(workflow_id, page_num)


def reset_page(workflow_id: str, page_num: int) -> Optional[dict]:
    """Reset a page to its original OCR markdown."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is not None:
            conn.execute(
                """
                UPDATE pages SET
                    edited_markdown = NULL,
                    metadata = (metadata - 'reviewer_notes') || '{"is_reviewed": false}'::jsonb
                WHERE document_id = %s::uuid AND page_number = %s
                """,
                (doc_uuid, page_num),
            )
    return get_page(workflow_id, page_num)
