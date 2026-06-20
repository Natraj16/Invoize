"""
Vision LLM extraction using Google Gemini 2.5 Flash.

This is "Path B" in our architecture: send the image DIRECTLY to a
vision-capable LLM and get structured JSON back. No OCR step needed.

Why this works better than OCR for messy receipts:
- Vision models see the LAYOUT, not just characters — they understand
  that a number on the right side of a line is probably a price
- They handle skewed/crumpled/low-contrast images that break Tesseract
- They can infer currency from symbols that OCR might misread (₹ vs $)

The key technical decision here is using `response_schema` (constrained
decoding) instead of prompting with "respond in JSON". This means:
- The model's token generation is RESTRICTED to valid JSON matching our schema
- We get guaranteed structure, not best-effort formatting
- Pydantic validation still runs as a safety net, but shouldn't ever fail
"""

import base64
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from app.config import settings
from app.schemas import ExtractionResponse, ReceiptData

# System prompt with few-shot guidance for tricky edge cases.
# These aren't full examples (the schema handles structure) — they're
# instructions for AMBIGUOUS situations the model might encounter.
EXTRACTION_PROMPT = """You are an expert receipt and invoice data extractor.

Extract ALL structured data from this receipt/invoice image. Be thorough and precise.

Key instructions for tricky cases:
- If line items don't have explicit quantities, assume quantity = 1
- If you see multiple tax lines (e.g., "State Tax" + "City Tax"), SUM them into the single `tax` field
- For dates: convert ANY format to YYYY-MM-DD (e.g., "06/15/24" → "2024-06-15", "15 Jun 2024" → "2024-06-15")
- For currency: infer from symbols and text ($ → USD, € → EUR, £ → GBP, ₹ → INR, ¥ → JPY, Rs → INR, Rs. → INR, Rupees → INR). Defaults to INR if Rupees/Rs or Indian location is present.
- If the receipt shows a discount line, include it as a line item with a NEGATIVE total_price
- If handwriting is unclear, extract your best reading — the validation layer will flag low-confidence fields
- The `total` field is the FINAL amount paid (after tax, after tips, after discounts)
- If subtotal is not explicitly printed but you can calculate it from line items, leave subtotal as null
  (our validation layer will compute it — don't guess)

Extract every visible field. Leave optional fields as null if not present on the receipt."""

TEXT_EXTRACTION_PROMPT = """You are an expert receipt and invoice data parser.

Extract ALL structured data from this receipt/invoice text (which may be formatted as CSV, JSON, or plain text). Be thorough and precise.

Key instructions for tricky cases:
- If line items don't have explicit quantities, assume quantity = 1
- If you see multiple tax lines (e.g., "State Tax" + "City Tax"), SUM them into the single `tax` field
- For dates: convert ANY format to YYYY-MM-DD (e.g., "06/15/24" → "2024-06-15", "15 Jun 2024" → "2024-06-15")
- For currency: infer from symbols ($ → USD, € → EUR, £ → GBP, ₹ → INR, Rs → INR). Default to INR if Rupees or ₹ is used or if no other currency is specified.
- If the receipt shows a discount, populate the `discount` field with the total discount amount (positive number). Also, if the discount is listed as a line item, include it as a line item with a NEGATIVE total_price.
- The `total` field is the FINAL amount paid (after tax, after tips, after discounts)
- If subtotal is not explicitly printed but you can calculate it from line items, leave subtotal as null

Extract every visible field. Leave optional fields as null if not present on the receipt."""


def _get_client() -> genai.Client:
    """
    Create a Gemini client.

    Why a function instead of a module-level global?
    - The API key might not be set when the module is first imported
    - This lets us fail with a clear error at call time, not import time
    - Makes testing easier (can mock this function)
    """
    if not settings.GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is not configured. "
            "Add it to your .env file (see .env.example)"
        )
    return genai.Client(api_key=settings.GEMINI_API_KEY)


def _load_image_as_part(file_path: Path) -> types.Part:
    """
    Load an image file and convert it to a Gemini-compatible Part.

    Uses base64 inline data instead of file upload API because:
    - No extra API call needed (upload → then reference)
    - Works within Gemini's free tier limits
    - Simpler error handling
    - Receipt images are small (< 4MB typically)
    """
    # Read raw bytes
    image_bytes = file_path.read_bytes()

    # Determine MIME type from the actual image, not just file extension
    # (defence against mislabeled files)
    img = Image.open(file_path)
    format_to_mime = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }
    mime_type = format_to_mime.get(img.format, "image/jpeg")

    return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)


def _load_image_bytes_as_part(image_bytes: bytes, mime_type: str) -> types.Part:
    """Load image from raw bytes (used when we already have bytes in memory)."""
    return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)


async def extract_from_image(
    image_source: Path | bytes,
    mime_type: str = "image/jpeg",
    filename: str = "unknown",
) -> ExtractionResponse:
    """
    Extract structured receipt data from an image using Gemini Vision.

    Args:
        image_source: Either a Path to an image file, or raw image bytes
        mime_type: MIME type of the image (used when image_source is bytes)
        filename: Original filename for the response metadata

    Returns:
        ExtractionResponse with extracted data or error details

    Why async even though genai.Client is sync?
    - FastAPI routes are async, and we want consistency
    - The actual Gemini call is I/O-bound (network), so it benefits from
      not blocking the event loop in production
    - For now we use sync client (Gemini SDK's async support is newer);
      we can swap to async client later without changing the interface
    """
    start_time = time.time()

    try:
        client = _get_client()

        # Build the image part
        if isinstance(image_source, Path):
            image_part = _load_image_as_part(image_source)
        else:
            image_part = _load_image_bytes_as_part(image_source, mime_type)

        # Call Gemini with structured output using retry helper
        from app.extraction.retry_helper import generate_content_with_retry
        response = await generate_content_with_retry(
            client=client,
            model=settings.GEMINI_MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=EXTRACTION_PROMPT),
                        image_part,
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReceiptData,
                temperature=0.1,  # Low temperature for deterministic extraction
            ),
        )

        # Parse the response — should always succeed due to constrained decoding,
        # but Pydantic validation is our safety net
        receipt_data = ReceiptData.model_validate_json(response.text)

        elapsed = time.time() - start_time

        return ExtractionResponse(
            success=True,
            filename=filename,
            extraction_method="vision_llm",
            data=receipt_data,
            processing_time_seconds=round(elapsed, 2),
        )

    except Exception as e:
        elapsed = time.time() - start_time
        return ExtractionResponse(
            success=False,
            filename=filename,
            extraction_method="vision_llm",
            error=f"{type(e).__name__}: {str(e)}",
            processing_time_seconds=round(elapsed, 2),
        )


async def extract_from_text(
    text_content: str,
    mime_type: str = "text/csv",
    filename: str = "unknown",
) -> ExtractionResponse:
    """
    Extract structured receipt data from text (CSV, JSON, plain text) using Gemini text generation.
    Bypasses the LLM for JSON/CSV matching supported structures directly.
    """
    start_time = time.time()

    try:
        # Attempt deterministic offline parsing first (JSON/CSV) to save API quota and speed up processing
        from app.extraction.text_parser import parse_text_deterministically
        receipt_data = parse_text_deterministically(text_content, filename, mime_type)
        if receipt_data:
            elapsed = time.time() - start_time
            ext_method = "direct_json" if ("json" in (mime_type or "").lower() or filename.endswith(".json")) else "direct_csv"
            return ExtractionResponse(
                success=True,
                filename=filename,
                extraction_method=ext_method,
                data=receipt_data,
                processing_time_seconds=round(elapsed, 2),
            )
    except Exception as e:
        # If offline parsing fails, fall back to LLM processing
        print(f"[INFO] Deterministic offline parsing failed: {e}. Falling back to Gemini.")
        pass

    try:
        client = _get_client()

        # Call Gemini with structured output using retry helper
        from app.extraction.retry_helper import generate_content_with_retry
        response = await generate_content_with_retry(
            client=client,
            model=settings.GEMINI_MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=TEXT_EXTRACTION_PROMPT),
                        types.Part.from_text(text=text_content),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReceiptData,
                temperature=0.1,
            ),
        )

        receipt_data = ReceiptData.model_validate_json(response.text)
        elapsed = time.time() - start_time

        return ExtractionResponse(
            success=True,
            filename=filename,
            extraction_method="text_llm",
            data=receipt_data,
            processing_time_seconds=round(elapsed, 2),
        )

    except Exception as e:
        elapsed = time.time() - start_time
        return ExtractionResponse(
            success=False,
            filename=filename,
            extraction_method="text_llm",
            error=f"{type(e).__name__}: {str(e)}",
            processing_time_seconds=round(elapsed, 2),
        )

