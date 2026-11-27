"""Verify a local DEV upload reaches Qdrant and can be retrieved by the API."""

import argparse
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

PASSAGE = (
    "This local test document describes how to prepare a small garden before planting. "
    "Start by measuring the bed and checking that water drains away after a rainstorm. "
    "Keep the young plants in an area with enough light and leave space for each plant. "
    "Check the soil each morning and record the date when new leaves start to grow. "
    "Use this record to compare how the plants respond to the same watering schedule."
)


def sample_pdf(run_code: str, passage: str = PASSAGE) -> bytes:
    """Make a one-page PDF with an embedded text layer for the local OCR provider."""
    commands = f"BT /F1 11 Tf 40 750 Td (Test reference {run_code}. {passage}) Tj ET".encode()
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2600 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(commands)} >>\nstream\n".encode() + commands + b"\nendstream",
    )
    pdf = bytearray(f"%PDF-1.4\n% {run_code}\n".encode())
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)


def run(api_url: str, qdrant_url: str, wait_seconds: int, qdrant_key_file: Path | None) -> None:
    for url in (api_url, qdrant_url):
        if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("The smoke test requires loopback API and Qdrant addresses")
    with httpx.Client(base_url=api_url, timeout=90, trust_env=False) as api:
        run_code = f"local-smoke-{uuid4().hex[:10]}"
        response = api.post(
            "/upload",
            params={
                "kind": "advisory",
                "code": run_code,
                "auto_approve": "true",
                "chunk_size": 150,
                "chunk_overlap": 20,
                "min_tokens": 10,
            },
            files={"file": (f"{run_code}.pdf", sample_pdf(run_code), "application/pdf")},
            data={"display_name": "Local garden smoke test"},
        )
        document = response.raise_for_status().json()
        workflow_id = document["workflow_id"]
        print(f"Uploaded {workflow_id}", flush=True)

        deadline = time.monotonic() + wait_seconds
        stage = None
        while time.monotonic() < deadline:
            document = api.get(f"/documents/{workflow_id}").raise_for_status().json()
            if document["stage"] != stage:
                stage = document["stage"]
                print(f"Workflow stage: {stage}", flush=True)
            if stage == "completed":
                break
            if stage in {"failed", "cancelled"}:
                raise RuntimeError(f"Workflow {workflow_id} ended at {stage}")
            time.sleep(2)
        else:
            raise TimeoutError(f"Workflow {workflow_id} did not complete (last stage: {stage})")

        headers = {"api-key": qdrant_key_file.read_text().strip()} if qdrant_key_file else {}
        with httpx.Client(
            base_url=qdrant_url, headers=headers, timeout=20, trust_env=False
        ) as qdrant:
            points = (
                qdrant.post(
                    "/collections/documents-index/points/scroll",
                    json={
                        "filter": {
                            "must": [{"key": "workflow_id", "match": {"value": workflow_id}}]
                        },
                        "with_payload": True,
                        "with_vector": True,
                        "limit": 10,
                    },
                )
                .raise_for_status()
                .json()["result"]["points"]
            )
        if not points:
            raise RuntimeError(f"Qdrant has no points for {workflow_id}")
        vector = points[0]["vector"]
        if isinstance(vector, dict):
            vector = next(iter(vector.values()), [])
        if not vector or not any(vector):
            raise RuntimeError("Qdrant point has no nonzero vector")
        print(f"Qdrant stored a {len(vector)}-dimension vector", flush=True)

        hits = (
            api.post(
                "/search",
                json={
                    "query": f"Test reference {run_code}. {PASSAGE}",
                    "search_mode": "TENSOR",
                    "top_k": 20,
                },
            )
            .raise_for_status()
            .json()["hits"]
        )
        if not any(hit.get("workflow_id") == workflow_id for hit in hits):
            raise RuntimeError(f"Search did not return {workflow_id}")
        print(f"PASS: upload, workflow, embedding, Qdrant, and search for {workflow_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8012")
    parser.add_argument("--qdrant", default="http://127.0.0.1:6333")
    parser.add_argument("--wait-seconds", type=int, default=420)
    parser.add_argument("--qdrant-key-file", type=Path)
    options = parser.parse_args()
    run(options.api, options.qdrant, options.wait_seconds, options.qdrant_key_file)
