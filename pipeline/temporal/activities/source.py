"""Activities for loading, normalizing and reading document sources."""

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pipeline.documents.document_formats import (
    BUILTIN_BINARY_SUFFIXES,
    TEXT_UPLOAD_TYPES,
    installed_formats,
)
from pipeline.documents.document_text import clean_text
from pipeline.intake.source_fetch import MAX_HTML_BYTES, extract_text_source
from pipeline.object_storage import is_object_uri
from pipeline.ocr import ocr_pdf as run_ocr_pdf
from pipeline.ocr import ocr_pdf_in_segments as run_ocr_pdf_in_segments
from pipeline.temporal.activities.artifacts import (
    _upload_artifact,
    _write_json_temp,
    download_object,
)

IMAGE_INPUT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


OFFICE_INPUT_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}


DELIMITED_INPUT_EXTENSIONS = {".csv"}


NATIVE_SPREADSHEET_EXTENSIONS = {".xlsx"}


def _source_type_from_path(filepath: str) -> tuple[str, str]:
    ext = Path(filepath).suffix.lower()
    if ext in TEXT_UPLOAD_TYPES:
        return ("document", "structured_text")
    if ext in {".csv", ".xlsx"}:
        return ("spreadsheet", "spreadsheet")
    if ext in BUILTIN_BINARY_SUFFIXES or any(item.suffix == ext for item in installed_formats()):
        return ("document", "pdf")
    return ("unknown", "pdf")


def _normalized_filename(original_name: str, canonical_input_type: str) -> str:
    stem = Path(original_name).stem or "document"
    if canonical_input_type == "spreadsheet":
        ext = Path(original_name).suffix.lower()
        return f"{stem}{ext if ext in {'.csv', '.xlsx'} else '.csv'}"
    return f"{stem}.pdf"


def _resolve_local_path(filepath: str) -> tuple[str, bool]:
    """
    Resolve path into local file path.
    Returns (path, should_cleanup).
    """
    if is_object_uri(filepath):
        local_path = download_object(filepath)
        activity.logger.info(f"Downloaded from MinIO to {local_path}")
        return local_path, True
    return filepath, False


def _convert_image_to_pdf(input_path: str, output_path: str) -> None:
    from PIL import Image, ImageOps

    with Image.open(input_path) as img:
        rgb_img = ImageOps.exif_transpose(img).convert("RGB")
        rgb_img.save(output_path, "PDF", resolution=300.0)


def _convert_office_to_pdf(input_path: str, output_dir: str) -> str:
    soffice_bin = shutil.which("soffice")
    if not soffice_bin:
        raise RuntimeError(
            "LibreOffice (soffice) is not installed. Install libreoffice to convert office files to PDF."
        )

    cmd = [
        soffice_bin,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        output_dir,
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to convert office document to PDF: {result.stderr.strip() or result.stdout.strip()}"
        )

    output_pdf = Path(output_dir) / f"{Path(input_path).stem}.pdf"
    if not output_pdf.exists():
        raise RuntimeError("Office-to-PDF conversion finished but output PDF was not found")
    return str(output_pdf)


def _ensure_pdf_input(local_path: str) -> tuple[str, bool]:
    """
    Ensure the given file path points to a PDF.
    Returns (pdf_path, should_cleanup).
    """
    ext = Path(local_path).suffix.lower()
    if ext == ".pdf":
        return local_path, False

    extension = next((item for item in installed_formats() if item.suffix == ext), None)
    if ext not in IMAGE_INPUT_EXTENSIONS | OFFICE_INPUT_EXTENSIONS and extension is None:
        raise ValueError(f"Unsupported file extension '{ext}'")

    work_dir = tempfile.mkdtemp(prefix="doc_convert_")
    output_pdf = Path(work_dir) / f"{Path(local_path).stem}.pdf"

    try:
        if ext in IMAGE_INPUT_EXTENSIONS:
            _convert_image_to_pdf(local_path, str(output_pdf))
        elif ext in OFFICE_INPUT_EXTENSIONS:
            converted = _convert_office_to_pdf(local_path, work_dir)
            if converted != str(output_pdf):
                shutil.move(converted, output_pdf)
        else:
            extension.normalize_to_pdf(local_path, str(output_pdf))
            with output_pdf.open("rb") as result:
                if result.read(5) != b"%PDF-":
                    raise ValueError("The document format extension did not produce a PDF")
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    activity.logger.info(f"Converted {local_path} -> {output_pdf}")
    return str(output_pdf), True


def _csv_to_pages(input_path: str, rows_per_page: int = 80) -> list[dict]:
    import csv

    def _open_reader():
        last_err = None
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                f = open(input_path, "r", encoding=enc, newline="")
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except Exception:
                    dialect = csv.excel
                return f, csv.reader(f, dialect=dialect)
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Could not read CSV file: {last_err}")

    f, reader = _open_reader()
    try:
        rows = [[(c or "").strip() for c in r] for r in reader if any((c or "").strip() for c in r)]
    finally:
        f.close()

    if not rows:
        return [{
            "page_number": 1,
            "original_markdown": f"# {Path(input_path).name}\n\n(Empty CSV file)",
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        }]

    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []

    pages: list[dict] = []
    page_num = 1
    for i in range(0, len(body), rows_per_page):
        batch = body[i:i + rows_per_page]
        lines = [f"# Spreadsheet Data: {Path(input_path).name}", "", f"Columns: {', '.join(header)}", ""]
        for row_idx, row in enumerate(batch, start=i + 1):
            pairs = []
            for col_i, val in enumerate(row):
                col = header[col_i] if col_i < len(header) and header[col_i] else f"col_{col_i+1}"
                pairs.append(f"{col}: {val}")
            if pairs:
                lines.append(f"- Row {row_idx}: " + " | ".join(pairs))
        pages.append({
            "page_number": page_num,
            "original_markdown": "\n".join(lines),
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        })
        page_num += 1
    return pages


def _xlsx_to_pages(input_path: str, rows_per_page: int = 80) -> list[dict]:
    from datetime import date, datetime

    from openpyxl import load_workbook

    def _cell_to_str(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return str(value).strip()

    pages: list[dict] = []
    page_num = 1
    wb = load_workbook(filename=input_path, data_only=True, read_only=True)

    for sheet in wb.worksheets:
        header: list[str] | None = None
        batch: list[tuple[int, list[str]]] = []
        seen_rows = 0

        def emit_page(rows_batch: list[tuple[int, list[str]]]) -> None:
            nonlocal page_num
            if not rows_batch:
                return
            assert header is not None
            lines = [
                f"# Spreadsheet Data: {Path(input_path).name}",
                "",
                f"Sheet: {sheet.title}",
                f"Columns: {', '.join(header)}",
                "",
            ]
            for excel_row_num, row in rows_batch:
                pairs = []
                for col_i, val in enumerate(row):
                    col = header[col_i] if col_i < len(header) and header[col_i] else f"col_{col_i+1}"
                    pairs.append(f"{col}: {val}")
                if pairs:
                    lines.append(f"- Row {excel_row_num}: " + " | ".join(pairs))
            pages.append({
                "page_number": page_num,
                "original_markdown": "\n".join(lines),
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            })
            page_num += 1

        for excel_row_num, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = [_cell_to_str(v) for v in row]
            if not any(values):
                continue
            seen_rows += 1

            if header is None:
                header = values
                continue

            batch.append((excel_row_num, values))
            if len(batch) >= rows_per_page:
                emit_page(batch)
                batch = []

        if header is not None:
            emit_page(batch)
        elif seen_rows == 0:
            pages.append({
                "page_number": page_num,
                "original_markdown": f"# Spreadsheet Data: {Path(input_path).name}\n\nSheet: {sheet.title}\n\n(Empty sheet)",
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            })
            page_num += 1

    wb.close()

    if not pages:
        return [{
            "page_number": 1,
            "original_markdown": f"# Spreadsheet Data: {Path(input_path).name}\n\n(Empty workbook)",
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        }]
    return pages


def _ocr_pdf(local_pdf_path: str) -> list[dict]:
    return run_ocr_pdf(local_pdf_path, clean_text)


def _ocr_pdf_in_segments(
    local_pdf_path: str,
    segment_pages: int | None,
    on_segment_complete=None,
    completed_page_numbers: set[int] | None = None,
    provider: str | None = None,
) -> list[dict]:
    return run_ocr_pdf_in_segments(
        local_pdf_path,
        segment_pages,
        clean_text,
        on_segment_complete=on_segment_complete,
        completed_page_numbers=completed_page_numbers,
        **({"provider": provider} if provider else {}),
    )


@activity.defn
async def run_ocr(filepath: str) -> list[dict]:
    """Run OCR on a supported file and return page dicts."""
    activity.logger.info(f"Running OCR on {filepath}")

    local_path, cleanup_local = _resolve_local_path(filepath)
    ext = Path(local_path).suffix.lower()
    pdf_path = local_path
    cleanup_pdf_dir = False

    try:
        if ext in DELIMITED_INPUT_EXTENSIONS:
            pages = _csv_to_pages(local_path)
            activity.logger.info(f"CSV parsed into {len(pages)} pages")
            return pages
        if ext in NATIVE_SPREADSHEET_EXTENSIONS:
            pages = _xlsx_to_pages(local_path)
            activity.logger.info(f"XLSX parsed into {len(pages)} pages")
            return pages
        pdf_path, cleanup_pdf_dir = await asyncio.to_thread(_ensure_pdf_input, local_path)
        return _ocr_pdf(pdf_path)
    finally:
        if cleanup_pdf_dir:
            try:
                shutil.rmtree(Path(pdf_path).parent, ignore_errors=True)
            except Exception:
                pass
        if cleanup_local and os.path.exists(local_path):
            os.remove(local_path)


@activity.defn
async def run_ocr_and_store(workflow_id: str, filepath: str, parser_provider: str | None = None) -> dict:
    """Run OCR and persist pages to Postgres to avoid large Temporal payloads."""
    from pipeline import db

    if filepath.startswith("intake://"):
        from pipeline.intake.source_intake import materialize_intake_pages

        return materialize_intake_pages(workflow_id, filepath)

    local_path, cleanup_local = _resolve_local_path(filepath)
    ext = Path(local_path).suffix.lower()
    source_type, canonical_input_type = _source_type_from_path(local_path)
    original_filename = Path(local_path).name
    normalized_path = local_path
    cleanup_normalized = False

    try:
        if ext in TEXT_UPLOAD_TYPES:
            with open(local_path, "rb") as raw:
                contents = raw.read(MAX_HTML_BYTES + 1)
            if ext == ".json":
                from pipeline.intake.source_intake import submitted_document_page
                from pipeline.intake.webhook_mapping import parse_canonical_document

                if len(contents) > MAX_HTML_BYTES:
                    raise ApplicationError("JSON documents must be at most 2 MiB", non_retryable=True)
                document = parse_canonical_document(contents)
                pages = [submitted_document_page(document.model_dump())]
            else:
                source = extract_text_source(contents, TEXT_UPLOAD_TYPES[ext], original_filename)
                pages = [{"page_number": 1, "original_markdown": source.text, "detected_language": source.language}]
        elif ext in DELIMITED_INPUT_EXTENSIONS:
            pages = _csv_to_pages(local_path)
        elif ext in NATIVE_SPREADSHEET_EXTENSIONS:
            pages = _xlsx_to_pages(local_path)
        else:
            normalized_path, cleanup_normalized = await asyncio.to_thread(_ensure_pdf_input, local_path)
            saved_page_numbers = set(db.get_saved_page_numbers(workflow_id))
            loop = asyncio.get_running_loop()

            def persist_segment(segment_pages_result: list[dict], total_pages: int) -> None:
                db.save_pages(workflow_id, segment_pages_result)
                current_saved = len(saved_page_numbers.union({p["page_number"] for p in segment_pages_result}))
                saved_page_numbers.update(p["page_number"] for p in segment_pages_result)
                db.update_document_fields(workflow_id, page_count=current_saved)
                # Heartbeat must run on the activity event loop (persist_segment runs in to_thread).
                loop.call_soon_threadsafe(
                    activity.heartbeat,
                    {"workflow_id": workflow_id, "pages_saved": current_saved, "total_pages": total_pages},
                )
                activity.logger.info(
                    "Persisted OCR segment for %s: %s/%s pages saved",
                    workflow_id,
                    current_saved,
                    total_pages,
                )

            pages = await asyncio.to_thread(
                _ocr_pdf_in_segments,
                normalized_path,
                segment_pages=None,
                on_segment_complete=persist_segment,
                completed_page_numbers=saved_page_numbers,
                **({"provider": parser_provider} if parser_provider else {}),
            )

            pages = db.get_pages(workflow_id)

        db.save_pages(workflow_id, pages)

        latest_job = db.get_latest_document_job(workflow_id)
        job_id = latest_job["id"] if latest_job else None

        artifact_type = {
            "structured_text": "normalized_text", "spreadsheet": "normalized_spreadsheet", "pdf": "normalized_pdf",
        }[canonical_input_type]
        if ext in TEXT_UPLOAD_TYPES:
            normalized_filename = f"{Path(original_filename).stem}.txt"
            with tempfile.TemporaryDirectory(prefix="kp-normalized-text-") as directory:
                text_path = Path(directory) / "document.txt"
                text_path.write_text(pages[0]["original_markdown"], encoding="utf-8")
                normalized_uri, normalized_size, normalized_mime = _upload_artifact(
                    str(text_path), workflow_id, artifact_type, normalized_filename,
                )
        else:
            normalized_filename = _normalized_filename(original_filename, canonical_input_type)
            normalized_uri, normalized_size, normalized_mime = _upload_artifact(
                normalized_path, workflow_id, artifact_type, normalized_filename,
            )
        normalized_artifact_id = db.add_document_artifact(
            workflow_id=workflow_id,
            job_id=job_id,
            artifact_type=artifact_type,
            stage="ocr_processing",
            storage_uri=normalized_uri,
            mime_type=normalized_mime,
            filename=normalized_filename,
            size_bytes=normalized_size,
            metadata={"source_filepath": filepath, "canonical_input_type": canonical_input_type},
        )

        if is_object_uri(filepath):
            original_uri = filepath
            original_size = None
            original_mime = "application/pdf" if ext == ".pdf" else "application/octet-stream"
        else:
            original_uri, original_size, original_mime = _upload_artifact(
                local_path,
                workflow_id,
                "original_upload",
                original_filename,
            )
        original_artifact_id = db.add_document_artifact(
            workflow_id=workflow_id,
            job_id=job_id,
            artifact_type="original_upload",
            stage="registered",
            storage_uri=original_uri,
            mime_type=original_mime,
            filename=original_filename,
            size_bytes=original_size,
            metadata={"source_filepath": filepath},
        )

        pages_json_path = _write_json_temp(pages)
        try:
            pages_uri, pages_size, pages_mime = _upload_artifact(
                pages_json_path, workflow_id, "ocr_pages_json", "pages.json"
            )
        finally:
            if os.path.exists(pages_json_path):
                os.remove(pages_json_path)

        db.add_document_artifact(
            workflow_id=workflow_id,
            job_id=job_id,
            artifact_type="ocr_pages_json",
            stage="ocr_review",
            storage_uri=pages_uri,
            mime_type=pages_mime,
            filename="pages.json",
            size_bytes=pages_size,
            metadata={"page_count": len(pages)},
        )

        db.update_document_fields(
            workflow_id,
            page_count=len(pages),
            source_type=source_type,
            canonical_input_type=canonical_input_type,
            original_artifact_id=original_artifact_id,
            normalized_artifact_id=normalized_artifact_id,
        )
        return {"page_count": len(pages), "normalized_artifact_id": normalized_artifact_id}
    finally:
        if cleanup_normalized and os.path.exists(normalized_path):
            shutil.rmtree(Path(normalized_path).parent, ignore_errors=True)
        if cleanup_local and os.path.exists(local_path):
            os.remove(local_path)
