"""
Validation & Confidence Scoring Layer.

THIS IS THE MOST IMPORTANT MODULE IN THE PROJECT.

Why? Because any LLM can extract text. What makes this project portfolio-worthy
is that it VALIDATES the extraction and FLAGS unreliable results instead of
silently returning wrong data.

Design philosophy:
- ALL validation is DETERMINISTIC — no LLM calls. If the LLM says 2+2=5,
  our math check catches it. Using the LLM to validate itself would be
  circular and unreliable.
- Every check produces a STRUCTURED flag, not just pass/fail. The flag
  includes what went wrong and why, so the user (or a downstream system)
  can decide what to do about it.
- Tolerance-aware: receipts have rounding. $12.76 + $0.89 = $13.65,
  but OCR might read $13.64. A ±$0.02 tolerance avoids false alarms.

Interview talking point: "The validation layer is completely independent
of the extraction layer. It doesn't trust the LLM — it verifies the LLM's
output using deterministic rules. This is a core principle of reliable
AI systems: never trust a model's output without independent verification."
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.schemas import ReceiptData


# --- Validation Result Models ---

class FieldFlag(BaseModel):
    """A flag on a specific field indicating a potential issue."""

    field_name: str = Field(description="Which field has the issue")
    severity: Literal["error", "warning", "info"] = Field(
        description=(
            "error = definitely wrong (math doesn't add up), "
            "warning = suspicious (date in future), "
            "info = minor note (optional field missing)"
        )
    )
    message: str = Field(description="Human-readable explanation of the issue")


class ValidationResult(BaseModel):
    """
    Complete validation output for an extracted receipt.

    This gets attached to the API response so the consumer knows
    HOW MUCH to trust the extraction.
    """

    is_valid: bool = Field(
        description="True if no errors found (warnings are OK)"
    )
    math_valid: bool = Field(
        description="True if all arithmetic checks pass"
    )
    flags: list[FieldFlag] = Field(
        default_factory=list,
        description="All issues found, sorted by severity",
    )
    overall_confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "high = all checks pass, "
            "medium = warnings but no errors, "
            "low = errors found or critical fields missing"
        )
    )
    needs_manual_review: bool = Field(
        description="True if the extraction should be reviewed by a human"
    )
    completeness_score: float = Field(
        description="0.0 to 1.0 — fraction of expected fields that are present"
    )


# --- Tolerance Constants ---

# Rounding tolerance for math checks (in currency units)
# Why $0.05? Real receipts have rounding from:
# - Tax calculation rounding (each line vs total)
# - Multi-currency conversion rounding
# - OCR misreading a digit
MATH_TOLERANCE = 0.05

# Maximum plausible receipt total (flag anything above this)
MAX_PLAUSIBLE_TOTAL = 50_000.0

# How far in the future a date can be before we flag it
MAX_FUTURE_DAYS = 1  # Allow 1 day for timezone differences


# --- Validation Functions ---

def _validate_math(data: ReceiptData) -> list[FieldFlag]:
    """
    Check arithmetic consistency of the extracted data.

    Three independent checks:
    1. Each line item: qty × unit_price ≈ total_price
    2. Sum of line items ≈ subtotal (if subtotal present)
    3. Subtotal + tax (+ tip) ≈ total

    Why tolerance-based instead of exact?
    - Tax is often calculated per-line then summed (rounding at each step)
    - Some stores round differently (banker's rounding vs truncation)
    - OCR/vision might misread a digit (e.g., 3 vs 8)
    """
    flags: list[FieldFlag] = []

    # Check 1: Line item arithmetic
    for i, item in enumerate(data.line_items):
        expected = round(item.quantity * item.unit_price, 2)
        actual = item.total_price
        diff = abs(expected - actual)

        if diff > MATH_TOLERANCE:
            flags.append(FieldFlag(
                field_name=f"line_items[{i}].total_price",
                severity="error",
                message=(
                    f"Line item '{item.name}': {item.quantity} x ${item.unit_price:.2f} "
                    f"= ${expected:.2f}, but total_price is ${actual:.2f} "
                    f"(difference: ${diff:.2f})"
                ),
            ))

    # Check 2: Line items sum ≈ subtotal
    if data.line_items and data.subtotal is not None:
        items_sum = round(sum(item.total_price for item in data.line_items), 2)
        diff = abs(items_sum - data.subtotal)

        if diff > MATH_TOLERANCE:
            flags.append(FieldFlag(
                field_name="subtotal",
                severity="error",
                message=(
                    f"Sum of line items (${items_sum:.2f}) doesn't match "
                    f"subtotal (${data.subtotal:.2f}), difference: ${diff:.2f}"
                ),
            ))

    # Check 3: Subtotal + tax + tip ≈ total
    if data.subtotal is not None:
        expected_total = data.subtotal
        if data.tax is not None:
            expected_total += data.tax
        if data.tip is not None:
            expected_total += data.tip
        expected_total = round(expected_total, 2)

        diff = abs(expected_total - data.total)
        if diff > MATH_TOLERANCE:
            components = f"subtotal (${data.subtotal:.2f})"
            if data.tax is not None:
                components += f" + tax (${data.tax:.2f})"
            if data.tip is not None:
                components += f" + tip (${data.tip:.2f})"

            flags.append(FieldFlag(
                field_name="total",
                severity="error",
                message=(
                    f"{components} = ${expected_total:.2f}, "
                    f"but total is ${data.total:.2f} "
                    f"(difference: ${diff:.2f})"
                ),
            ))

    # Check 3b: If no subtotal, check line items + tax ≈ total
    elif data.line_items and data.subtotal is None:
        items_sum = round(sum(item.total_price for item in data.line_items), 2)
        expected_total = items_sum
        if data.tax is not None:
            expected_total += data.tax
        if data.tip is not None:
            expected_total += data.tip
        expected_total = round(expected_total, 2)

        diff = abs(expected_total - data.total)
        if diff > MATH_TOLERANCE:
            flags.append(FieldFlag(
                field_name="total",
                severity="warning",
                message=(
                    f"Sum of line items (${items_sum:.2f}) + tax/tip "
                    f"= ${expected_total:.2f}, but total is ${data.total:.2f} "
                    f"(difference: ${diff:.2f}). Note: subtotal was missing."
                ),
            ))

    return flags


def _validate_fields(data: ReceiptData) -> list[FieldFlag]:
    """
    Check field-level plausibility.

    These are heuristic checks — they flag SUSPICIOUS values, not
    necessarily wrong ones. A $50,000 receipt might be legitimate
    (bulk order), but it's worth flagging for review.
    """
    flags: list[FieldFlag] = []

    # --- Date checks ---
    if data.date:
        try:
            parsed_date = datetime.strptime(data.date, "%Y-%m-%d")

            # Future date?
            if parsed_date.date() > (datetime.now() + timedelta(days=MAX_FUTURE_DAYS)).date():
                flags.append(FieldFlag(
                    field_name="date",
                    severity="warning",
                    message=f"Date {data.date} is in the future",
                ))

            # Very old date? (more than 10 years ago)
            if parsed_date.date() < (datetime.now() - timedelta(days=3650)).date():
                flags.append(FieldFlag(
                    field_name="date",
                    severity="warning",
                    message=f"Date {data.date} is more than 10 years ago",
                ))

        except ValueError:
            flags.append(FieldFlag(
                field_name="date",
                severity="warning",
                message=f"Date '{data.date}' is not in expected YYYY-MM-DD format",
            ))

    # --- Currency checks ---
    # ISO 4217 currency codes are exactly 3 uppercase letters
    valid_currencies = {
        "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR",
        "MXN", "BRL", "KRW", "SGD", "HKD", "NOK", "SEK", "DKK", "NZD",
        "ZAR", "RUB", "TRY", "THB", "MYR", "PHP", "IDR", "VND", "AED",
        "SAR", "TWD", "PLN", "CZK", "HUF", "ILS", "CLP", "ARS", "COP",
        "PEN", "EGP", "NGN", "KES", "GHS", "PKR", "BDT", "LKR",
    }
    if data.currency and data.currency.upper() not in valid_currencies:
        # Check if it's at least 3 uppercase letters (might be a valid code we don't know)
        if not re.match(r"^[A-Z]{3}$", data.currency):
            flags.append(FieldFlag(
                field_name="currency",
                severity="warning",
                message=f"Currency '{data.currency}' doesn't look like a valid ISO 4217 code",
            ))

    # --- Total checks ---
    if data.total < 0:
        flags.append(FieldFlag(
            field_name="total",
            severity="warning",
            message=f"Total is negative (${data.total:.2f}). Is this a refund?",
        ))

    if data.total == 0:
        flags.append(FieldFlag(
            field_name="total",
            severity="warning",
            message="Total is $0.00 — possibly an error",
        ))

    if data.total > MAX_PLAUSIBLE_TOTAL:
        flags.append(FieldFlag(
            field_name="total",
            severity="warning",
            message=f"Total (${data.total:,.2f}) is unusually high. Please verify.",
        ))

    # --- Line item checks ---
    if not data.line_items:
        flags.append(FieldFlag(
            field_name="line_items",
            severity="warning",
            message="No line items extracted. The receipt might have items that weren't detected.",
        ))

    for i, item in enumerate(data.line_items):
        if item.quantity <= 0:
            flags.append(FieldFlag(
                field_name=f"line_items[{i}].quantity",
                severity="warning",
                message=f"Item '{item.name}' has quantity {item.quantity} (zero or negative)",
            ))

        if item.unit_price < 0:
            # Negative prices can be legitimate (discounts), but flag them
            flags.append(FieldFlag(
                field_name=f"line_items[{i}].unit_price",
                severity="info",
                message=f"Item '{item.name}' has negative unit price ${item.unit_price:.2f} (discount?)",
            ))

    # --- Tax checks ---
    if data.tax is not None and data.subtotal is not None and data.subtotal > 0:
        tax_rate = data.tax / data.subtotal
        if tax_rate > 0.30:  # 30% tax rate is very unusual
            flags.append(FieldFlag(
                field_name="tax",
                severity="warning",
                message=(
                    f"Effective tax rate is {tax_rate:.1%} "
                    f"(${data.tax:.2f} on ${data.subtotal:.2f}). "
                    f"This seems unusually high."
                ),
            ))

    return flags


def _check_completeness(data: ReceiptData) -> tuple[float, list[FieldFlag]]:
    """
    Score how complete the extraction is.

    Returns (score, flags) where score is 0.0 to 1.0.

    Fields are weighted by importance:
    - Required (vendor_name, total): missing = major issue
    - Important (date, line_items, currency): missing = notable
    - Optional (address, time, tip, payment_method): missing = fine
    """
    flags: list[FieldFlag] = []

    # Define fields and their weights
    checks = [
        # (field_name, value, weight, is_required)
        ("vendor_name", data.vendor_name, 2.0, True),
        ("total", data.total, 2.0, True),  # total is always present (required in schema)
        ("date", data.date, 1.5, False),
        ("line_items", data.line_items, 1.5, False),
        ("currency", data.currency, 1.0, False),
        ("subtotal", data.subtotal, 1.0, False),
        ("tax", data.tax, 0.5, False),
        ("vendor_address", data.vendor_address, 0.5, False),
        ("time", data.time, 0.25, False),
        ("payment_method", data.payment_method, 0.25, False),
        ("tip", data.tip, 0.25, False),
    ]

    total_weight = sum(w for _, _, w, _ in checks)
    achieved_weight = 0.0

    for field_name, value, weight, is_required in checks:
        # Check if the field has a meaningful value
        is_present = bool(value)  # handles None, empty string, empty list
        if field_name == "total":
            is_present = True  # total is always present (Pydantic required)

        if is_present:
            achieved_weight += weight
        else:
            if is_required:
                flags.append(FieldFlag(
                    field_name=field_name,
                    severity="error",
                    message=f"Required field '{field_name}' is missing or empty",
                ))
            elif weight >= 1.0:
                flags.append(FieldFlag(
                    field_name=field_name,
                    severity="info",
                    message=f"Field '{field_name}' was not found on the receipt",
                ))

    score = achieved_weight / total_weight if total_weight > 0 else 0.0
    return round(score, 2), flags


# --- Main Validation Entry Point ---

def validate_receipt(data: ReceiptData) -> ValidationResult:
    """
    Run all validation checks on extracted receipt data.

    This is the single entry point — call this from the API route
    after extraction. It runs all three check categories and produces
    a unified ValidationResult.

    Example:
        result = validate_receipt(extracted_data)
        if result.needs_manual_review:
            # Flag for human review
        else:
            # Safe to auto-process
    """
    all_flags: list[FieldFlag] = []

    # 1. Math checks (deterministic, most reliable)
    math_flags = _validate_math(data)
    all_flags.extend(math_flags)
    math_valid = not any(f.severity == "error" for f in math_flags)

    # 2. Field plausibility checks (heuristic)
    field_flags = _validate_fields(data)
    all_flags.extend(field_flags)

    # 3. Completeness scoring
    completeness_score, completeness_flags = _check_completeness(data)
    all_flags.extend(completeness_flags)

    # Sort flags: errors first, then warnings, then info
    severity_order = {"error": 0, "warning": 1, "info": 2}
    all_flags.sort(key=lambda f: severity_order.get(f.severity, 3))

    # Determine overall confidence
    error_count = sum(1 for f in all_flags if f.severity == "error")
    warning_count = sum(1 for f in all_flags if f.severity == "warning")

    if error_count > 0 or completeness_score < 0.5:
        overall_confidence = "low"
    elif warning_count > 2 or completeness_score < 0.7:
        overall_confidence = "medium"
    else:
        overall_confidence = "high"

    # Determine if manual review is needed
    needs_review = (
        overall_confidence == "low"
        or error_count > 0
        or (warning_count >= 3)
    )

    is_valid = error_count == 0

    return ValidationResult(
        is_valid=is_valid,
        math_valid=math_valid,
        flags=all_flags,
        overall_confidence=overall_confidence,
        needs_manual_review=needs_review,
        completeness_score=completeness_score,
    )
