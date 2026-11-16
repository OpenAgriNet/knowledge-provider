"""PostgreSQL query API for the application and migration tools."""

from .artifacts import (
    add_document_artifact as add_document_artifact,
    get_document_artifact as get_document_artifact,
    list_document_artifacts as list_document_artifacts,
)
from .audit import (
    get_all_audit_log_count as get_all_audit_log_count,
    get_all_audit_logs as get_all_audit_logs,
    get_audit_log_count as get_audit_log_count,
    get_audit_logs as get_audit_logs,
    log_audit as log_audit,
    meaningful_roles as meaningful_roles,
)
from .catalog import (
    CATALOG_VERSION_ACTION as CATALOG_VERSION_ACTION,
    bump_catalog_version as bump_catalog_version,
    ensure_instance_catalogs as ensure_instance_catalogs,
    get_catalog_entry as get_catalog_entry,
    get_catalog_meta as get_catalog_meta,
    get_or_create_catalog as get_or_create_catalog,
    list_catalog_entries as list_catalog_entries,
    list_document_codes as list_document_codes,
    list_scheme_catalog_entries_for_workflow as list_scheme_catalog_entries_for_workflow,
    list_used_scheme_codes as list_used_scheme_codes,
    sync_catalogs_for_all_instances as sync_catalogs_for_all_instances,
)
from .chunks import (
    get_chunk as get_chunk,
    get_chunks as get_chunks,
    reset_chunk as reset_chunk,
    save_chunks as save_chunks,
    search_chunks as search_chunks,
    update_chunk as update_chunk,
)
from .connection import (
    DEFAULT_DATABASE_URL as DEFAULT_DATABASE_URL,
    check_health as check_health,
    close_pool as close_pool,
    database_url as database_url,
    get_connection as get_connection,
    get_pool as get_pool,
    init_db as init_db,
)
from .document_reads import (
    find_document_by_code as find_document_by_code,
    get_document as get_document,
    get_document_count as get_document_count,
    get_document_counts_by_instance as get_document_counts_by_instance,
    get_document_source_url as get_document_source_url,
    get_document_summary_counts as get_document_summary_counts,
    list_documents as list_documents,
    owned_source_fingerprint as owned_source_fingerprint,
)
from .document_state import (
    reconcile_materialized_state as reconcile_materialized_state,
    update_document_stage as update_document_stage,
)
from .document_writes import (
    DocumentReplacementConflict as DocumentReplacementConflict,
    DuplicateDocumentCode as DuplicateDocumentCode,
    clear_prod_ready_requested as clear_prod_ready_requested,
    delete_document as delete_document,
    mark_document_reindex_required as mark_document_reindex_required,
    mark_prod_ready_requested as mark_prod_ready_requested,
    purge_document as purge_document,
    reset_document_for_reupload as reset_document_for_reupload,
    set_document_disabled as set_document_disabled,
    set_document_validity as set_document_validity,
    update_document_fields as update_document_fields,
    upsert_document as upsert_document,
)
from .index_state import (
    DEV_INDEX_KEY as DEV_INDEX_KEY,
    PROD_INDEX_KEY as PROD_INDEX_KEY,
    clear_superseded_doc_ids as clear_superseded_doc_ids,
    doc_id_used_by_other_document as doc_id_used_by_other_document,
    find_document_by_doc_identifier as find_document_by_doc_identifier,
    get_document_index_status as get_document_index_status,
    inactive_network_revision_ids as inactive_network_revision_ids,
    list_document_index_status as list_document_index_status,
    list_index_summaries as list_index_summaries,
    resolve_chunk_provenance as resolve_chunk_provenance,
    upsert_document_index_status as upsert_document_index_status,
    visible_document_hits as visible_document_hits,
    visible_release_hits as visible_release_hits,
)
from .jobs import (
    create_document_job as create_document_job,
    get_document_job as get_document_job,
    get_latest_document_job as get_latest_document_job,
    list_document_jobs as list_document_jobs,
    list_operations_queue as list_operations_queue,
    list_runs as list_runs,
    mark_upload_started as mark_upload_started,
    update_document_job as update_document_job,
)
from .pages import (
    PageTranslationMissing as PageTranslationMissing,
    count_translated_pages as count_translated_pages,
    get_page as get_page,
    get_pages as get_pages,
    get_saved_page_numbers as get_saved_page_numbers,
    page_translation_key as page_translation_key,
    persist_document_content as persist_document_content,
    reset_page as reset_page,
    save_pages as save_pages,
    update_page as update_page,
)
from .providers import (
    get_or_create_instance as get_or_create_instance,
    get_user_by_keycloak_id as get_user_by_keycloak_id,
    list_provider_instances as list_provider_instances,
    upsert_instance as upsert_instance,
    upsert_user as upsert_user,
)
from .revisions import (
    activate_network_release as activate_network_release,
    get_latest_completed_index_revision as get_latest_completed_index_revision,
    get_revision_snapshot as get_revision_snapshot,
    list_document_revisions as list_document_revisions,
    load_revision_material as load_revision_material,
    save_draft_revision_snapshot as save_draft_revision_snapshot,
    save_legacy_index_revisions as save_legacy_index_revisions,
    save_revision_snapshot as save_revision_snapshot,
    select_network_revision as select_network_revision,
    stage_saved_network_revision as stage_saved_network_revision,
    withdraw_network_release as withdraw_network_release,
)
from .settings import (
    get_search_settings as get_search_settings,
    get_settings_audit_count as get_settings_audit_count,
    get_settings_audit_logs as get_settings_audit_logs,
    update_search_settings as update_search_settings,
)
