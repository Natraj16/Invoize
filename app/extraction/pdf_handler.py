"""
PDF → Image conversion handler.

Receipts and invoices often come as PDFs (scanned documents, email attachments,
digital receipts). This module converts PDF pages to images so they can be
processed by the same vision pipeline as uploaded photos.

Why PyMuPDF (fitz) instead of pdf2image/poppler?
- Pure Python — no system-level dependency to install (poppler is painful on Windows)
- Fast: C-extension based, converts pages in milliseconds
- Small footprint: doesn't pull in Ghostscript or other heavy deps
- Good quality: renders at configurable DPI

Design decision: We extract ALL pages and process each one separately.
A multi-page invoice might have line items on page 1 and totals on page 2,
but for MVP we treat each page as a separate "receipt" — a user can see all
pages and pick the right one. Multi-page merging is a Phase 5+ enhancement.
"""

from io import BytesIO
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image


def pdf_to_images(
    pdf_source: Path | bytes,
    dpi: int = 200,
) -> list[tuple[bytes, str]]:
    """
    Convert a PDF to a list of PNG images (one per page).

    Args:
        pdf_source: Path to PDF file, or raw PDF bytes
        dpi: Resolution for rendering. 200 DPI is a good balance:
             - 72 DPI (screen) → too low for OCR or small text
             - 300 DPI → best quality but larger files, slower
             - 200 DPI → good enough for Gemini Vision, fast

    Returns:
        List of (image_bytes, mime_type) tuples, one per page.
        Images are PNG format for lossless quality.

    Raises:
        ValueError: If the PDF is empty or corrupted
    """
    # Open the PDF
    if isinstance(pdf_source, Path):
        doc = fitz.open(str(pdf_source))
    else:
        doc = fitz.open(stream=pdf_source, filetype="pdf")

    if doc.page_count == 0:
        doc.close()
        raise ValueError("PDF has no pages")

    images: list[tuple[bytes, str]] = []

    # The zoom factor converts DPI:
    # fitz default is 72 DPI, so zoom = target_dpi / 72
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in range(doc.page_count):
        page = doc[page_num]

        # Render page to a pixmap (raster image)
        pixmap = page.get_pixmap(matrix=matrix)

        # Convert to PIL Image, then to PNG bytes
        img = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        images.append((buffer.getvalue(), "image/png"))

    doc.close()
    return images
