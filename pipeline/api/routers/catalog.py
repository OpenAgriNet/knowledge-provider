import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from pipeline import db
from pipeline.network.catalog import scheme_catalog
from pipeline.auth.deps import CurrentUser, RequireAdmin
from pipeline.auth.models import AuthUser
from pipeline.auth.tenancy import allowed_instances, normalize_instance
from pipeline.api import api as _api

router = APIRouter()


def _catalog_service_keys() -> list[str]:
    raw = (os.environ.get("CATALOG_SERVICE_API_KEYS") or "").strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def _service_key_ok(request: Request) -> bool:
    import hmac

    keys = _catalog_service_keys()
    if not keys:
        return False
    header = (
        request.headers.get("X-Catalog-Service-Key")
        or request.headers.get("x-catalog-service-key")
        or ""
    ).strip()
    if not header:
        return False
    return any(hmac.compare_digest(header, k) for k in keys)


def _catalog_read_scope(request: Request, user: AuthUser, instance: str | None = None) -> set[str] | None:
    """Return the provider scope for a catalog read after checking access."""
    from pipeline.auth.permissions import Permission

    service_key = _service_key_ok(request)
    if not service_key and not (user.has_permission(Permission.ADMIN) or user.has_permission(Permission.SEARCH)):
        if _catalog_service_keys():
            raise HTTPException(401, "Missing or invalid X-Catalog-Service-Key")
        raise HTTPException(403, "Catalog access requires search or admin permission")
    scope = allowed_instances(user)
    if service_key and not user.token_disabled_mode:
        machine_scope = {
            normalize_instance(code) for code in os.environ.get("CATALOG_SERVICE_PROVIDER_SCOPE", "").split(",")
            if code.strip()
        }
        if not machine_scope:
            raise HTTPException(403, "No provider scope is configured for the catalog service")
        scope = machine_scope if scope is None else scope & machine_scope
    if instance and scope is not None and normalize_instance(instance) not in scope:
        raise HTTPException(403, "No access to the requested provider")
    return scope


@router.get("/catalog/v1/version")
async def catalog_version(request: Request, user: CurrentUser):
    """Cheap poll for cache warmers: { version, updated_at }."""
    _catalog_read_scope(request, user)
    meta = db.get_catalog_meta()
    return {
        "version": int(meta.get("version") or 0),
        "updated_at": meta.get("updated_at"),
        "notes": meta.get("notes"),
    }


@router.get("/catalog/v1/schemes")
async def catalog_schemes(
    request: Request,
    user: CurrentUser,
    instance: Optional[str] = Query(None),
    status: str = Query("live"),
    include_pending: bool = Query(False),
    network_visible: Optional[bool] = Query(None),
):
    """List schemes from the master catalog (vector-indexed + filters)."""
    scope = _catalog_read_scope(request, user, instance)
    snap = scheme_catalog.build_snapshot(
        instance=instance,
        allowed_providers=scope,
        status=status,
        include_pending=include_pending,
        network_visible=network_visible,
    )
    return {
        "catalog_version": snap["catalog_version"],
        "updated_at": snap["updated_at"],
        "vector_schemes": snap["vector_schemes"],
        "legacy_schemes": snap["legacy_schemes"],
        "routing_exceptions": snap["routing_exceptions"],
    }


@router.get("/catalog/v1/snapshot")
async def catalog_snapshot(
    request: Request,
    user: CurrentUser,
    instance: Optional[str] = Query(None),
    status: str = Query("live"),
    include_pending: bool = Query(False),
    network_visible: Optional[bool] = Query(None),
):
    """
    Full catalog snapshot for OAN / provider warmers.

    Includes vector_schemes, legacy_schemes, tool_prompts, routing_exceptions,
    collections metadata, and monotonic catalog_version.
    """
    scope = _catalog_read_scope(request, user, instance)
    return scheme_catalog.build_snapshot(
        instance=instance,
        allowed_providers=scope,
        status=status,
        include_pending=include_pending,
        network_visible=network_visible,
    )


@router.get("/catalog/v1/tool-prompt")
async def catalog_tool_prompt(request: Request, user: CurrentUser):
    """Prompt fragments only (for AI system prompt assembly)."""
    scope = _catalog_read_scope(request, user)
    snap = scheme_catalog.build_snapshot(allowed_providers=scope)
    return {
        "catalog_version": snap["catalog_version"],
        "updated_at": snap["updated_at"],
        "tool_prompts": snap["tool_prompts"],
        "routing_exceptions": snap["routing_exceptions"],
        "vector_scheme_codes": [s["scheme_code"] for s in snap["vector_schemes"]],
        "legacy_scheme_codes": [s["scheme_code"] for s in snap["legacy_schemes"]],
    }


@router.post("/catalog/v1/rebuild")
async def catalog_rebuild(request: Request, user: RequireAdmin):
    """Admin: bump the catalog version (entries are computed from documents)."""
    from pipeline.auth.permissions import Permission

    # Admin JWT always OK; service key also accepted for automation
    if not _service_key_ok(request) and not user.has_permission(Permission.ADMIN):
        raise HTTPException(403, "Rebuild requires admin")
    version = scheme_catalog.rebuild_catalog_from_documents(reason="admin_rebuild")
    snap = scheme_catalog.build_snapshot()
    _api._log_audit(
        workflow_id="__catalog__",
        action_type="catalog_rebuild",
        entity_type="catalog",
        metadata={"catalog_version": version, "vector_count": len(snap["vector_schemes"])},
        user=user,
    )
    return {
        "ok": True,
        "catalog_version": version,
        "vector_scheme_count": len(snap["vector_schemes"]),
        "updated_at": snap["updated_at"],
    }
