from fastapi import APIRouter, HTTPException, Query

from pipeline import db
from pipeline.auth.deps import RequireAdmin, RequireSearch
from pipeline.documents.models import SearchSettings, SearchSettingsUpdate, SettingsAuditResponse

router = APIRouter()


@router.get("/settings/search", response_model=SearchSettings)
async def get_search_settings(user: RequireSearch):
    """
    Get current search settings.

    Read-only search configuration. Gated with ``RequireSearch`` (not
    ``RequireAdmin``) because any search-capable user needs these defaults
    (index name, method, limits) to drive the search UI; mutating the
    settings (PUT / reset) still requires ``RequireAdmin``.

    Returns the current search configuration including:
    - searchMethod: TENSOR, LEXICAL, or HYBRID
    - limit: Number of results to return
    - alpha: Balance between lexical (0) and semantic (1) for hybrid search
    - rankingMethod: rrf or normalize_linear for hybrid search
    - showHighlights: Whether to show highlighted matches
    - efSearch: HNSW search accuracy parameter
    """
    return db.get_search_settings()


@router.put("/settings/search", response_model=SearchSettings)
async def update_search_settings_endpoint(settings: SearchSettingsUpdate, user: RequireAdmin):
    """
    Update search settings.

    Only provided fields will be updated. Changes are logged to the audit trail.
    """
    _ = user
    # Convert to dict, excluding None values
    updates = {k: v for k, v in settings.model_dump().items() if v is not None}

    if not updates:
        raise HTTPException(400, "No settings provided to update")

    try:
        return db.update_search_settings(updates)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/settings/search/audit", response_model=SettingsAuditResponse)
async def get_search_settings_audit(
    user: RequireAdmin,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for search settings changes.

    Shows all historical changes to search settings with old/new values.
    """
    logs = db.get_settings_audit_logs(limit=limit, offset=offset)
    total = db.get_settings_audit_count()

    return SettingsAuditResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@router.post("/settings/search/reset", response_model=SearchSettings)
async def reset_search_settings(user: RequireAdmin):
    """
    Reset search settings to defaults.

    Resets all search settings to their default values:
    - searchMethod: HYBRID
    - limit: 10
    - alpha: 0.7
    - rankingMethod: rrf
    - showHighlights: true
    - efSearch: 256
    """
    _ = user
    defaults = {
        "searchMethod": "HYBRID",
        "limit": 12,
        "alpha": 0.6,
        "rankingMethod": "rrf",
        "showHighlights": True,
        "efSearch": 256,
        "indexName": "documents-index",
        "candidateCap": 120,
        "candidateMultiplier": 10,
        "maxChunksPerDoc": 2,
        "useE5Prefix": True,
        "excludeReference": True,
        "queryExpansionProfile": "none",
        "rerankMode": "none",
        "hybridRrfK": 60,
    }
    return db.update_search_settings(defaults)
