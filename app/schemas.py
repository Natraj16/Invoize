"""
Pydantic schemas for receipt/invoice data extraction.

These models serve a DUAL purpose:
1. They define the data contract for our API responses (standard Pydantic usage)
2. They are passed DIRECTLY to Gemini as `response_schema`, which uses constrained
   decoding to guarantee the LLM output matches this exact structure.

This is fundamentally different from prompting with "please respond in JSON":
- Constrained decoding restricts token generation at inference time
- Malformed JSON is literally impossible
- Required fields cannot be omitted
- Type enforcement (string vs number) is guaranteed

Interview talking point: "The schema is the single source of truth for both
the LLM extraction and the API response. If I add a field to the schema,
it automatically gets extracted AND returned — no prompt editing needed."
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    """A single purchased item on a receipt."""

    name: str = Field(
        description="Product or service name exactly as it appears on the receipt"
    )
    quantity: float = Field(
        default=1.0,
        description="Quantity purchased. Default 1 if not explicitly listed.",
    )
    unit_price: float = Field(
        description="Price per single unit before any line-level discounts"
    )
    total_price: float = Field(
        description="Line total (typically quantity × unit_price). "
        "Use the printed value if visible, even if it doesn't match the multiplication."
    )


class ReceiptData(BaseModel):
    """
    Complete structured representation of a receipt or invoice.

    Field descriptions are critical — they're sent to the LLM as part of the
    JSON schema and significantly affect extraction quality. Each description
    tells the model WHERE to look and WHAT format to use.
    """

    vendor_name: str = Field(
        description="Business or store name, typically at the top of the receipt"
    )
    vendor_address: Optional[str] = Field(
        default=None,
        description="Full address of the business if visible on the receipt",
    )
    date: Optional[str] = Field(
        default=None,
        description="Transaction date in YYYY-MM-DD format. "
        "Convert from any format (MM/DD/YY, DD.MM.YYYY, etc.) to ISO 8601.",
    )
    time: Optional[str] = Field(
        default=None,
        description="Transaction time in HH:MM format (24-hour) if visible",
    )
    currency: str = Field(
        default="INR",
        description="3-letter ISO 4217 currency code. "
        "Infer from currency symbols ($ → USD, € → EUR, £ → GBP, ₹ → INR, Rs → INR) "
        "or explicit text on the receipt. Defaults to INR if not specified.",
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="List of individual items purchased. "
        "Include every line item visible on the receipt.",
    )
    subtotal: Optional[float] = Field(
        default=None,
        description="Subtotal before tax, as printed on the receipt",
    )
    discount: Optional[float] = Field(
        default=0.0,
        description="Any total discount amount applied to the entire bill if visible, else 0.0",
    )
    tax: Optional[float] = Field(
        default=None,
        description="Total tax amount. If multiple tax lines exist, sum them.",
    )
    tip: Optional[float] = Field(
        default=None,
        description="Tip or gratuity amount, if applicable (restaurants, services)",
    )
    total: float = Field(
        description="Final total amount paid, including tax and tip"
    )
    payment_method: Optional[str] = Field(
        default=None,
        description="Payment method if visible (e.g., VISA, CASH, MASTERCARD, UPI)",
    )


class ExtractionResponse(BaseModel):
    """
    API response wrapping extracted data with metadata.

    Why wrap ReceiptData instead of returning it directly?
    - We need to include metadata (filename, extraction method, processing time)
    - The validation layer adds confidence flags here
    - Keeps the extraction schema clean and reusable
    """

    success: bool = Field(description="Whether extraction completed without errors")
    filename: str = Field(description="Original uploaded filename")
    extraction_method: str = Field(
        default="vision_llm",
        description="Which extraction path was used: 'vision_llm' or 'ocr_llm'",
    )
    id: Optional[str] = Field(
        default=None,
        description="Database record ID if successfully saved to storage",
    )
    data: Optional[ReceiptData] = Field(
        default=None,
        description="Extracted receipt data, null if extraction failed",
    )
    validation: Optional[dict] = Field(
        default=None,
        description="Validation results with confidence flags. "
        "Contains: is_valid, math_valid, flags[], overall_confidence, "
        "needs_manual_review, completeness_score",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if extraction failed",
    )
    processing_time_seconds: Optional[float] = Field(
        default=None,
        description="Wall-clock time for the full extraction pipeline",
    )


def get_currency_symbol(currency_code: str) -> str:
    """Return the corresponding currency symbol for an ISO 4217 code."""
    symbols = {
        "INR": "₹",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CAD": "C$",
        "AUD": "A$",
    }
    code = (currency_code or "").strip().upper()
    if code in ("RS", "RUPEES", "INR", "₹"):
        return "₹"
    return symbols.get(code, "$")

