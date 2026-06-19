"""
OCR-based extraction pipeline: preprocess image (OpenCV) → OCR (Tesseract) → Clean up & structure (Gemini).

This is "Path A" in our architecture, serving as a hybrid comparison point
to the direct vision path. It extracts text locally via Tesseract, then uses
Gemini as an intelligent parser to organize the raw OCR text into our structured schema.

Why use this hybrid path?
- Privacy: The image itself is processed locally; only extracted text goes to the cloud.
- Bandwidth: Sending raw text requires much less bandwidth than sending high-resolution images.
- Reliability comparison: Demonstrates the difference in accuracy between vision models
  and standard OCR engines for messy layouts.
"""

import os
import time
from pathlib import Path
from typing import Optional
import numpy as np
import cv2
import pytesseract
from PIL import Image
from google import genai
from google.genai import types

from app.config import settings
from app.schemas import ExtractionResponse, ReceiptData

# Configure Tesseract path for Windows if installed in standard location
TESSERACT_WINDOWS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.name == "nt" and os.path.exists(TESSERACT_WINDOWS_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_WINDOWS_PATH

OCR_CLEANUP_PROMPT = """You are an expert receipt and invoice data parser.

We have extracted raw text from a receipt/invoice using local OCR. The raw text is messy, may contain typos, OCR errors (like '$' read as 's' or '5', or '1' read as 'l'), and line layout shifts.

Analyze the raw OCR text and structure it into the output schema. Clean up typos and correct obvious OCR errors based on context.

Raw OCR Text:
---
{ocr_text}
---

Key instructions:
- If line items don't have explicit quantities, assume quantity = 1.0
- If you see multiple tax lines, SUM them into the single `tax` field
- Convert dates to YYYY-MM-DD format (e.g., "06/15/24" → "2024-06-15")
- Infer currency from symbols or context (e.g., '$' → USD, '£' → GBP, '€' → EUR, '₹' → INR)
- If there's a discount, include it as a line item with a NEGATIVE total_price
- The `total` field must be the FINAL total amount paid
- If subtotal is not printed, leave it null (do not compute it yourself, our validation layer does that)
- Leave optional fields null if you cannot find them in the text.
"""


def preprocess_image(image_source: Path | bytes, mime_type: str = "image/jpeg") -> np.ndarray:
    """
    Preprocess image using OpenCV to improve OCR accuracy.
    - Convert to Grayscale
    - Upscale if width is too small (< 1500px)
    - Denoise using Bilateral Filter (preserves edges)
    - Adaptive Thresholding to handle uneven lighting
    """
    # 1. Read image as numpy array
    if isinstance(image_source, Path):
        img = cv2.imread(str(image_source))
    else:
        nparr = np.frombuffer(image_source, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        raise ValueError("Could not decode image file.")

    # 2. Grayscale conversion
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 3. Resize/Upscale if too small (Tesseract needs sufficient pixel height for characters)
    height, width = gray.shape
    if width < 1500:
        scale = 1500 / width
        new_width = int(width * scale)
        new_height = int(height * scale)
        gray = cv2.resize(gray, (new_width, new_height), interpolation=cv2.INTER_CUBIC)

    # 4. Bilateral Filtering (denoising while keeping text edges sharp)
    denoised = cv2.bilateralFilter(gray, 9, 75, 75)

    # 5. Adaptive Thresholding (turns image black & white to handle shadows/uneven light)
    processed = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )

    return processed


def extract_raw_ocr_text(processed_img: np.ndarray) -> str:
    """Run Tesseract OCR on preprocessed image."""
    try:
        # Check if tesseract command is callable or configured
        # This will raise a TesseractNotFoundError if not installed/configured
        # We run it with default language (English) and page segmentation mode 6 (Assume a single uniform block of text)
        config = "--psm 6"
        return pytesseract.image_to_string(processed_img, config=config)
    except pytesseract.TesseractNotFoundError:
        # Check standard location again just in case, or raise with user guidance
        raise RuntimeError(
            "Tesseract OCR is not installed or the executable was not found. "
            "Please install Tesseract (e.g. via 'winget install UB-Mannheim.TesseractOCR') "
            f"and make sure it is in your system PATH or installed at '{TESSERACT_WINDOWS_PATH}'."
        )


def _get_client() -> genai.Client:
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured.")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


async def extract_via_ocr_pipeline(
    image_source: Path | bytes,
    mime_type: str = "image/jpeg",
    filename: str = "unknown",
) -> ExtractionResponse:
    """
    Perform local OCR extraction on image, then call Gemini to structure the text.
    """
    start_time = time.time()

    try:
        # 1. Preprocess the image
        processed_img = preprocess_image(image_source, mime_type)

        # 2. Run Tesseract OCR locally
        ocr_text = extract_raw_ocr_text(processed_img)
        if not ocr_text.strip():
            raise ValueError("OCR extracted empty text. The image might be too blurry or corrupted.")

        # 3. Call Gemini to parse and structure the OCR text
        client = _get_client()
        prompt = OCR_CLEANUP_PROMPT.format(ocr_text=ocr_text)

        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
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
            extraction_method="ocr_llm",
            data=receipt_data,
            processing_time_seconds=round(elapsed, 2),
        )

    except Exception as e:
        elapsed = time.time() - start_time
        return ExtractionResponse(
            success=False,
            filename=filename,
            extraction_method="ocr_llm",
            error=f"{type(e).__name__}: {str(e)}",
            processing_time_seconds=round(elapsed, 2),
        )
