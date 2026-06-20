"""
FastAPI application — the API layer for the Invoize parser.

Routes:
    POST /upload         — Upload a single receipt image/PDF → structured JSON
    POST /batch-upload   — Upload multiple files → list of results
    GET  /health         — Health check (useful for monitoring + demo)

Design decisions:
- File validation happens BEFORE any LLM call (fail fast, save API credits)
- We save uploaded files to disk so we have a record for debugging and
  for the storage layer (Phase 4) to reference
- Async route handlers for non-blocking I/O (FastAPI best practice)
- Detailed error messages — a user uploading a 50MB video should get
  "File too large (50MB). Maximum is 10MB." not "400 Bad Request"
"""

import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, Query
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.extraction.pdf_handler import pdf_to_images
from app.extraction.vision_llm import extract_from_image, extract_from_text
from app.extraction.ocr import extract_via_ocr_pipeline
from app.schemas import ExtractionResponse
from app.validation import validate_receipt
from app import storage
from app.export import generate_csv, generate_excel

# --- App Setup ---

app = FastAPI(
    title="Invoize API",
    description=(
        "AI-powered receipt and invoice parser. "
        "Upload an image or PDF → get structured JSON with vendor, "
        "date, line items, tax, total, and confidence flags."
    ),
    version="0.1.0",
)

# CORS: allow Gradio frontend (and any other local dev tools) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Fine for local dev; lock down in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Startup Validation ---

@app.on_event("startup")
async def validate_config():
    """
    Check configuration at startup, not at first request.
    This way you see errors immediately in the terminal, not after
    uploading a file and waiting for the LLM call to fail.
    """
    errors = settings.validate()
    if errors:
        for err in errors:
            print(f"[WARNING] CONFIG: {err}")
        print("The app will start, but extraction will fail until config is fixed.")
    else:
        print(f"[OK] Config loaded. Model: {settings.GEMINI_MODEL}")
        print(f"[OK] Upload directory: {settings.UPLOAD_DIR}")

    # Initialize database
    storage.init_db()
    print(f"[OK] Database: {settings.DB_PATH}")


# --- Helper Functions ---

async def _validate_upload(file: UploadFile) -> bytes:
    """
    Validate an uploaded file and return its bytes.

    Validation order matters — cheapest checks first:
    1. MIME type check (instant, no I/O)
    2. Read bytes + size check
    3. File integrity check (try to open as image/PDF)

    Why not just let the LLM handle bad files?
    - LLM calls cost time (and eventually money)
    - A clear "unsupported file type" error is better UX than
      "the model couldn't extract any data"
    - Defence in depth: don't send arbitrary files to external APIs
    """
    # 1. Check MIME type and fallback to extension check
    content_type = file.content_type
    ext = Path(file.filename or "").suffix.lower()
    ext_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".csv": "text/csv",
        ".json": "application/json",
        ".txt": "text/plain",
    }
    
    if content_type not in settings.ALLOWED_MIME_TYPES or content_type == "application/octet-stream":
        if ext in ext_map:
            content_type = ext_map[ext]
            file.content_type = content_type  # Update file object content-type
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file type or extension: {content_type} ({ext or 'no extension'}). "
                    f"Allowed extensions: {', '.join(sorted(ext_map.keys()))}"
                ),
            )

    # 2. Read and check size
    contents = await file.read()
    if len(contents) > settings.MAX_FILE_SIZE_BYTES:
        size_mb = len(contents) / (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large ({size_mb:.1f}MB). "
                f"Maximum is {settings.MAX_FILE_SIZE_MB}MB."
            ),
        )

    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")

    # 3. Integrity check — try to open/parse the file
    is_text = (
        file.content_type in ("text/csv", "application/json", "text/plain")
        or (file.filename and file.filename.lower().endswith((".csv", ".json")))
    )
    if is_text:
        try:
            text = contents.decode("utf-8")
            if file.content_type == "application/json" or (file.filename and file.filename.lower().endswith(".json")):
                import json
                json.loads(text)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid text or JSON content: {str(e)}",
            )
    elif file.content_type == "application/pdf":
        try:
            # Quick check: can we open it as a PDF?
            import fitz
            doc = fitz.open(stream=contents, filetype="pdf")
            if doc.page_count == 0:
                raise ValueError("PDF has no pages")
            doc.close()
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Corrupted or invalid PDF: {str(e)}",
            )
    else:
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(contents))
            img.verify()  # Check file integrity without fully loading
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Corrupted or invalid image: {str(e)}",
            )

    return contents


def _save_upload(contents: bytes, original_filename: str) -> Path:
    """
    Save uploaded file to disk with a UUID prefix to avoid collisions.

    Returns the path to the saved file.

    Why save to disk?
    - Debugging: if extraction is wrong, we can re-run on the same file
    - Storage layer (Phase 4) needs a file reference
    - Audit trail: know exactly what was processed
    """
    # Preserve original extension for clarity
    ext = Path(original_filename).suffix or ".bin"
    safe_name = f"{uuid.uuid4().hex[:12]}_{original_filename}"
    save_path = settings.UPLOAD_DIR / safe_name
    save_path.write_bytes(contents)
    return save_path


async def _process_single_file(
    file: UploadFile,
    method: str = "vision_llm",
) -> ExtractionResponse:
    """
    Full pipeline for a single file: validate → save → extract.

    This function handles both images and PDFs transparently.
    For PDFs, it converts each page to an image and extracts from the first page.
    (Multi-page extraction is a future enhancement.)
    """
    # Validate
    contents = await _validate_upload(file)

    # Save
    save_path = _save_upload(contents, file.filename or "upload")

    # Extract
    is_text = (
        file.content_type in ("text/csv", "application/json", "text/plain")
        or (file.filename and file.filename.lower().endswith((".csv", ".json")))
    )
    if is_text:
        text_content = contents.decode("utf-8")
        result = await extract_from_text(
            text_content=text_content,
            mime_type=file.content_type or "text/csv",
            filename=file.filename or "unknown",
        )
    elif file.content_type == "application/pdf":
        # Convert PDF pages to images
        page_images = pdf_to_images(contents)
        if not page_images:
            return ExtractionResponse(
                success=False,
                filename=file.filename or "unknown",
                error="PDF conversion produced no images",
            )

        # Extract from first page (MVP — most receipts are single-page)
        image_bytes, mime_type = page_images[0]
        if method == "ocr_llm":
            result = await extract_via_ocr_pipeline(
                image_source=image_bytes,
                mime_type=mime_type,
                filename=file.filename or "unknown",
            )
        else:
            result = await extract_from_image(
                image_source=image_bytes,
                mime_type=mime_type,
                filename=file.filename or "unknown",
            )
    else:
        # Direct image extraction
        if method == "ocr_llm":
            result = await extract_via_ocr_pipeline(
                image_source=contents,
                mime_type=file.content_type or "image/jpeg",
                filename=file.filename or "unknown",
            )
        else:
            result = await extract_from_image(
                image_source=contents,
                mime_type=file.content_type or "image/jpeg",
                filename=file.filename or "unknown",
            )

    # Run validation on successful extractions
    if result.success and result.data:
        validation = validate_receipt(result.data)
        result.validation = validation.model_dump()

    # Save to database
    if result.success:
        try:
            receipt_id = storage.save_receipt(result)
            result.id = receipt_id
        except Exception as e:
            # Don't fail the whole request if storage fails
            print(f"[WARNING] Failed to save receipt: {e}")

    return result


# --- Routes ---

@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns config status so you can quickly verify the API is running
    and properly configured before uploading files.
    """
    config_errors = settings.validate()
    return {
        "status": "healthy" if not config_errors else "degraded",
        "model": settings.GEMINI_MODEL,
        "max_file_size_mb": settings.MAX_FILE_SIZE_MB,
        "config_errors": config_errors,
    }


@app.post("/upload", response_model=ExtractionResponse)
async def upload_receipt(
    file: Annotated[UploadFile, File(description="Receipt image or PDF")],
    method: Annotated[str, Query(description="Extraction method: 'vision_llm' or 'ocr_llm'")] = "vision_llm",
):
    """
    Upload a single receipt/invoice and extract structured data.

    Accepts: JPEG, PNG, WebP images and PDF files.
    Returns: Structured JSON with vendor, date, line items, tax, total.
    """
    if method not in ("vision_llm", "ocr_llm"):
        raise HTTPException(status_code=400, detail="Invalid extraction method. Choose 'vision_llm' or 'ocr_llm'.")
    return await _process_single_file(file, method)


@app.post("/batch-upload", response_model=list[ExtractionResponse])
async def batch_upload_receipts(
    files: Annotated[
        list[UploadFile],
        File(description="Multiple receipt images or PDFs"),
    ],
    method: Annotated[str, Query(description="Extraction method: 'vision_llm' or 'ocr_llm'")] = "vision_llm",
):
    """
    Upload multiple receipts and extract structured data from each.

    Processes files sequentially (not parallel) to respect API rate limits
    on the free tier. Each file is independent — one failure doesn't
    affect the others.
    """
    if method not in ("vision_llm", "ocr_llm"):
        raise HTTPException(status_code=400, detail="Invalid extraction method. Choose 'vision_llm' or 'ocr_llm'.")
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    if len(files) > 20:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files ({len(files)}). Maximum is 20 per batch.",
        )

    results: list[ExtractionResponse] = []
    for file in files:
        result = await _process_single_file(file, method)
        results.append(result)

    return results


@app.get("/receipts")
async def list_stored_receipts(
    confidence: Optional[str] = None,
    needs_review: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
):
    """
    List all stored receipts with optional filters.

    Query params:
    - confidence: "high", "medium", or "low"
    - needs_review: true/false
    - limit: max results (default 50)
    - offset: pagination offset
    """
    receipts = storage.list_receipts(
        confidence=confidence,
        needs_review=needs_review,
        limit=limit,
        offset=offset,
    )
    total = storage.get_receipt_count()
    return {
        "receipts": receipts,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/receipts/{receipt_id}")
async def get_stored_receipt(receipt_id: str):
    """Get a single stored receipt by ID."""
    receipt = storage.get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@app.get("/export/csv")
async def export_csv(receipt_id: Optional[str] = None):
    """
    Export receipts as CSV.

    Returns a downloadable CSV file with one row per line item.
    Optionally filter to a single receipt by ID.
    """
    from fastapi.responses import Response

    csv_content = generate_csv(receipt_id=receipt_id)
    filename = f"receipts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/excel")
async def export_excel(receipt_id: Optional[str] = None):
    """
    Export receipts as Excel workbook.

    Returns a downloadable .xlsx file with two sheets:
    - Receipts (summary)
    - Line Items (detail)
    """
    from fastapi.responses import Response

    excel_bytes = generate_excel(receipt_id=receipt_id)
    filename = f"receipts_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Mount Gradio UI for production hosting ---
import gradio as gr
from frontend.app import demo

app = gr.mount_gradio_app(app, demo, path="/")
