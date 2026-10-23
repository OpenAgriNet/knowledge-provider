#!/usr/bin/env python3
import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from qdrant_client.http import models as qmodels  # noqa: E402

from pipeline import db  # noqa: E402
from pipeline.auth.tenancy import group_for_instance  # noqa: E402
from pipeline.network_group import load_provider_naming, named_group, provider_for  # noqa: E402
from pipeline.network_provider import InvalidProviderIdError, ProviderNaming  # noqa: E402
from pipeline.vector_store import get_default_index_name  # noqa: E402
from pipeline.vector_store.qdrant_store import get_qdrant_client  # noqa: E402

PROVIDER_ID_FIELD = "provider_id"
PAGE_SIZE = 500


@dataclass(frozen=True)
class Target:
    label: str
    client: Any
    collection: str


@dataclass(frozen=True)
class PlannedDocument:
    workflow_id: str
    provider_id: Optional[str]
    error: Optional[str] = None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def targets_from_env() -> list[Target]:
    targets = [Target("dev", get_qdrant_client(), get_default_index_name())]
    prod_url = _env("PROD_VECTOR_DB_URL")
    if not prod_url:
        return targets
    prod_key = _env("PROD_VECTOR_DB_API_KEY") or None
    prod_collection = _env("PROD_VECTOR_DB_COLLECTION_NAME") or get_default_index_name()
    targets.append(Target("prod", get_qdrant_client(url=prod_url, api_key=prod_key), prod_collection))
    scheme_url = _env("PROD_SCHEME_VECTOR_DB_URL") or prod_url
    scheme_key = _env("PROD_SCHEME_VECTOR_DB_API_KEY") or prod_key
    scheme_collection = _env("PROD_SCHEME_VECTOR_DB_COLLECTION_NAME") or "schemes-index"
    targets.append(Target("prod-schemes", get_qdrant_client(url=scheme_url, api_key=scheme_key), scheme_collection))
    return targets


def all_documents() -> Iterable[dict]:
    offset = 0
    while True:
        page = db.list_documents(limit=PAGE_SIZE, offset=offset, include_demo=True, include_disabled=True)
        yield from page
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def plan(documents: Iterable[dict], naming: ProviderNaming) -> list[PlannedDocument]:
    planned = []
    for doc in documents:
        group = named_group(group_for_instance(doc.get("instance")))
        try:
            provider = provider_for(naming, doc.get("document_kind"), group)
        except InvalidProviderIdError as exc:
            planned.append(PlannedDocument(doc["workflow_id"], None, str(exc)))
            continue
        planned.append(PlannedDocument(doc["workflow_id"], provider["id"] if provider else None))
    return planned


def _workflow_filter(workflow_id: str) -> qmodels.Filter:
    return qmodels.Filter(
        must=[qmodels.FieldCondition(key="workflow_id", match=qmodels.MatchValue(value=workflow_id))]
    )


def backfill(target: Target, planned: list[PlannedDocument], apply: bool) -> dict:
    stats = {"documents": 0, "points_set": 0, "points_cleared": 0, "skipped_invalid": 0}
    if not target.client.collection_exists(target.collection):
        stats["missing_collection"] = True
        return stats
    if apply:
        target.client.create_payload_index(
            collection_name=target.collection,
            field_name=PROVIDER_ID_FIELD,
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
    for document in planned:
        if document.error:
            stats["skipped_invalid"] += 1
            continue
        selector = _workflow_filter(document.workflow_id)
        count = target.client.count(target.collection, count_filter=selector, exact=True).count
        if not count:
            continue
        stats["documents"] += 1
        if document.provider_id:
            stats["points_set"] += count
            if apply:
                target.client.set_payload(
                    collection_name=target.collection,
                    payload={PROVIDER_ID_FIELD: document.provider_id},
                    points=selector,
                )
        else:
            stats["points_cleared"] += count
            if apply:
                target.client.delete_payload(
                    collection_name=target.collection,
                    keys=[PROVIDER_ID_FIELD],
                    points=selector,
                )
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Set provider_id on existing DEV and PROD chunks to the provider id each document "
            "publishes under; clear it for kinds that never publish. Dry run unless --apply."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Write the payload changes.")
    args = parser.parse_args(argv)

    naming = load_provider_naming()
    if naming.network_id is None:
        print("NETWORK_SENDER_ID is not set; provider ids can't be worked out, nothing was changed.")
        return 1
    planned = plan(all_documents(), naming)
    for document in planned:
        if document.error:
            print(f"invalid provider id, skipped: {document.workflow_id}: {document.error}")

    mode = "apply" if args.apply else "dry run"
    for target in targets_from_env():
        stats = backfill(target, planned, args.apply)
        print(f"[{mode}] {target.label} {target.collection}: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
