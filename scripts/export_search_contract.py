"""Refresh the published search request schema from its runtime model."""

from pathlib import Path

import yaml

from pipeline.search.models import SearchRequest


def request_components() -> dict:
    schema = SearchRequest.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = schema.pop("$defs", {})
    return {**definitions, "SearchRequest": schema}


def export() -> None:
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi-search.yaml"
    document = yaml.safe_load(target.read_text())
    document["openapi"] = "3.1.0"
    document["info"].update({
        "title": "Knowledge Provider Search API",
        "description": "Search and chunk lookup. SearchRequest is generated from the runtime model by scripts/export_search_contract.py. See docs/SEARCH.md for authorization, ranking and BV examples.",
        "version": "1.1.0",
    })
    document["components"]["schemas"].update(request_components())
    examples = document["paths"]["/search"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    examples["bv_recorded_query"] = {"summary": "Recorded BV question with exact scheme identity", "value": {
        "query": "PM Kisan overview", "group": "bv", "document_kind": "scheme", "document_code": "pk",
        "search_mode": "TENSOR", "top_k": 10, "candidate_cap": 80, "max_chunks_per_doc": 10,
    }}
    examples["network_release"] = {"summary": "Authorized serving network; requires a provisioned principal grant", "value": {
        **examples["bv_recorded_query"]["value"], "network_id": "oan.dev",
    }}
    target.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=120))


if __name__ == "__main__":
    export()
