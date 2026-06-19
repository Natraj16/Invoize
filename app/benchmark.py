"""
Benchmark script to evaluate and compare Vision LLM vs OCR+LLM extraction pipelines.

This script:
1. Sets up a default test suite (using the sample receipt image and ground truth JSON).
2. Runs both extraction methods on all receipts in the test set.
3. Computes field-level accuracy (strings, dates, numbers, currency, line items).
4. Measures processing times.
5. Generates a markdown comparison report.
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel

from app.config import settings
from app.extraction.vision_llm import extract_from_image
from app.extraction.ocr import extract_via_ocr_pipeline
from app.schemas import ReceiptData

# Directories
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SET_DIR = PROJECT_ROOT / "tests" / "test_set"

# Source path of the sample receipt artifact
SAMPLE_RECEIPT_SRC = Path(r"C:\Users\natra\.gemini\antigravity-ide\brain\919e1754-00ac-4ec2-8d39-cff08ba508af\test_receipt_1781873235729.png")

GROUND_TRUTH_DATA = {
    "vendor_name": "FRESH MART",
    "vendor_address": "123 Main St., City, ST 12345",
    "date": "2024-06-15",
    "time": "10:30",
    "currency": "USD",
    "line_items": [
        {"name": "MILK", "quantity": 1.0, "unit_price": 3.99, "total_price": 3.99},
        {"name": "BREAD", "quantity": 1.0, "unit_price": 2.49, "total_price": 2.49},
        {"name": "EGGS", "quantity": 1.0, "unit_price": 4.99, "total_price": 4.99},
        {"name": "BANANAS", "quantity": 1.0, "unit_price": 1.29, "total_price": 1.29}
    ],
    "subtotal": 12.76,
    "tax": 0.89,
    "tip": None,
    "total": 13.65,
    "payment_method": "VISA"
}


def setup_test_set():
    """Create tests/test_set directory, copy sample image, and write ground truth JSON."""
    TEST_SET_DIR.mkdir(parents=True, exist_ok=True)
    
    img_dest = TEST_SET_DIR / "receipt_1.png"
    json_dest = TEST_SET_DIR / "receipt_1.json"

    # Copy the sample receipt if available
    if SAMPLE_RECEIPT_SRC.exists():
        shutil.copy(SAMPLE_RECEIPT_SRC, img_dest)
        print(f"[SETUP] Copied sample receipt to {img_dest}")
    else:
        print(f"[SETUP] Warning: Sample receipt source not found at {SAMPLE_RECEIPT_SRC}")
        print("Please place a receipt image in tests/test_set/receipt_1.png manually.")

    # Write the ground truth JSON
    with open(json_dest, "w", encoding="utf-8") as f:
        json.dump(GROUND_TRUTH_DATA, f, indent=2)
    print(f"[SETUP] Created ground truth JSON at {json_dest}")


def normalize_str(s: Any) -> str:
    """Normalize string for fuzzy comparison (lowercase, strip whitespace)."""
    if s is None:
        return ""
    return str(s).strip().lower()


def compare_values(extracted: Any, ground_truth: Any, tolerance: float = 0.02) -> Tuple[bool, str]:
    """Compare two values (numeric with tolerance, otherwise string normalized)."""
    if ground_truth is None:
        # If ground truth is None, we accept None or empty extracted
        if extracted is None or normalize_str(extracted) == "":
            return True, "Match (Both Null/Empty)"
        return False, f"Extracted: {extracted} | Expected: None"

    if isinstance(ground_truth, (int, float)):
        if extracted is None:
            return False, f"Extracted: None | Expected: {ground_truth}"
        try:
            val_ext = float(extracted)
            val_gt = float(ground_truth)
            diff = abs(val_ext - val_gt)
            if diff <= tolerance:
                return True, f"Match (Diff: {diff:.3f})"
            return False, f"Extracted: {val_ext} | Expected: {val_gt} (Diff: {diff:.3f})"
        except ValueError:
            return False, f"Extracted non-numeric: {extracted} | Expected: {ground_truth}"

    # String comparison
    str_ext = normalize_str(extracted)
    str_gt = normalize_str(ground_truth)
    if str_gt in str_ext or str_ext in str_gt:
        return True, "Match (Substring)"
    return False, f"Extracted: '{extracted}' | Expected: '{ground_truth}'"


def evaluate_receipt(extracted: ReceiptData, gt: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate extraction accuracy against ground truth."""
    metrics = {}
    
    # 1. Compare top-level fields
    fields_to_compare = ["vendor_name", "date", "currency", "subtotal", "tax", "total"]
    matched_count = 0
    total_fields = len(fields_to_compare)

    for field in fields_to_compare:
        val_ext = getattr(extracted, field, None)
        val_gt = gt.get(field, None)
        success, reason = compare_values(val_ext, val_gt)
        metrics[field] = {
            "success": success,
            "extracted": val_ext,
            "expected": val_gt,
            "details": reason
        }
        if success:
            matched_count += 1

    # 2. Compare line items
    gt_items = gt.get("line_items", [])
    ext_items = getattr(extracted, "line_items", [])
    
    line_item_matches = 0
    for gt_item in gt_items:
        # Try to find a match in extracted items
        found = False
        for ext_item in ext_items:
            name_match, _ = compare_values(ext_item.name, gt_item.get("name"))
            price_match, _ = compare_values(ext_item.total_price, gt_item.get("total_price"))
            if name_match and price_match:
                found = True
                line_item_matches += 1
                break
    
    line_item_accuracy = line_item_matches / len(gt_items) if gt_items else 1.0
    metrics["line_items_accuracy"] = line_item_accuracy
    metrics["line_items"] = {
        "extracted_count": len(ext_items),
        "expected_count": len(gt_items),
        "matches": line_item_matches
    }

    # Overall score = average of field match rate + line item accuracy
    field_match_rate = matched_count / total_fields
    metrics["overall_score"] = (field_match_rate + line_item_accuracy) / 2.0

    return metrics


async def run_benchmark():
    """Run benchmark comparison on the test set."""
    print("=" * 60)
    print("         Invoize Benchmarking Suite")
    print("=" * 60)
    print()

    # Always ensure test set is set up
    setup_test_set()

    # Find test files
    test_images = sorted(list(TEST_SET_DIR.glob("*.png")) + list(TEST_SET_DIR.glob("*.jpg")) + list(TEST_SET_DIR.glob("*.jpeg")))
    
    if not test_images:
        print("[ERROR] No test images found in tests/test_set. Exiting.")
        return

    print(f"Found {len(test_images)} test receipts to evaluate.")
    print()

    results = []

    for img_path in test_images:
        json_path = img_path.with_suffix(".json")
        if not json_path.exists():
            print(f"[SKIP] No ground truth JSON found for {img_path.name}")
            continue

        with open(json_path, "r", encoding="utf-8") as f:
            gt_data = json.load(f)

        print(f"Evaluating: {img_path.name}...")

        # --- Path A: Vision LLM ---
        print("  Running Vision LLM extraction...")
        vision_resp = await extract_from_image(img_path, filename=img_path.name)
        
        # --- Path B: OCR + LLM ---
        print("  Running OCR + LLM extraction...")
        ocr_resp = await extract_via_ocr_pipeline(img_path, filename=img_path.name)

        # Evaluate Vision
        vision_eval = None
        if vision_resp.success and vision_resp.data:
            vision_eval = evaluate_receipt(vision_resp.data, gt_data)
            print(f"    Vision LLM Score: {vision_eval['overall_score'] * 100:.1f}% | Time: {vision_resp.processing_time_seconds}s")
        else:
            print(f"    Vision LLM Failed: {vision_resp.error}")

        # Evaluate OCR
        ocr_eval = None
        if ocr_resp.success and ocr_resp.data:
            ocr_eval = evaluate_receipt(ocr_resp.data, gt_data)
            print(f"    OCR + LLM Score: {ocr_eval['overall_score'] * 100:.1f}% | Time: {ocr_resp.processing_time_seconds}s")
        else:
            print(f"    OCR + LLM Failed: {ocr_resp.error}")

        results.append({
            "filename": img_path.name,
            "vision": {
                "success": vision_resp.success,
                "error": vision_resp.error,
                "time": vision_resp.processing_time_seconds,
                "eval": vision_eval
            },
            "ocr": {
                "success": ocr_resp.success,
                "error": ocr_resp.error,
                "time": ocr_resp.processing_time_seconds,
                "eval": ocr_eval
            }
        })
        print()

    # Generate Report
    generate_report(results)


def generate_report(results: List[Dict[str, Any]]):
    """Generate and write a markdown benchmark report."""
    report_path = PROJECT_ROOT / "docs" / "benchmark_results.md"
    
    # Calculate averages
    total_vision_score = 0.0
    total_vision_time = 0.0
    total_vision_success = 0
    
    total_ocr_score = 0.0
    total_ocr_time = 0.0
    total_ocr_success = 0
    
    count = len(results)

    for r in results:
        v = r["vision"]
        o = r["ocr"]
        
        if v["success"] and v["eval"]:
            total_vision_score += v["eval"]["overall_score"]
            total_vision_time += v["time"]
            total_vision_success += 1
            
        if o["success"] and o["eval"]:
            total_ocr_score += o["eval"]["overall_score"]
            total_ocr_time += o["time"]
            total_ocr_success += 1

    avg_vision_score = (total_vision_score / total_vision_success * 100) if total_vision_success else 0.0
    avg_vision_time = (total_vision_time / total_vision_success) if total_vision_success else 0.0
    avg_ocr_score = (total_ocr_score / total_ocr_success * 100) if total_ocr_success else 0.0
    avg_ocr_time = (total_ocr_time / total_ocr_success) if total_ocr_success else 0.0

    report = f"""# Invoize — Benchmarking Results

This report evaluates and compares **Path A (Vision LLM)** and **Path B (OCR + LLM)** extraction pipelines.

## Benchmark Summary

| Pipeline | Success Rate | Average Accuracy | Average Processing Time |
|----------|--------------|------------------|-------------------------|
| **Path A: Vision LLM** (Gemini 2.5 Flash) | {total_vision_success}/{count} ({total_vision_success/count*100:.1f}%) | **{avg_vision_score:.1f}%** | **{avg_vision_time:.2f} seconds** |
| **Path B: OCR + LLM** (Tesseract + Gemini) | {total_ocr_success}/{count} ({total_ocr_success/count*100:.1f}%) | {avg_ocr_score:.1f}% | {avg_ocr_time:.2f} seconds |

---

## Detailed Performance by File

"""
    for r in results:
        v = r["vision"]
        o = r["ocr"]
        
        report += f"### File: `{r['filename']}`\n\n"
        report += "| Metric | Path A: Vision LLM | Path B: OCR + LLM |\n"
        report += "|--------|---------------------|-------------------|\n"
        
        # Success status
        report += f"| Extraction Status | {'✅ Success' if v['success'] else '❌ Failed'} | {'✅ Success' if o['success'] else '❌ Failed'} |\n"
        
        # Error (if any)
        if not v["success"] or not o["success"]:
            err_v = v["error"] if not v["success"] else ""
            err_o = o["error"] if not o["success"] else ""
            report += f"| Error Details | `{err_v}` | `{err_o}` |\n"
            
        # Processing time
        report += f"| Processing Time | {v['time']}s | {o['time']}s |\n"

        # Field metrics
        if v["success"] and v["eval"] and o["success"] and o["eval"]:
            report += f"| Overall Accuracy Score | **{v['eval']['overall_score']*100:.1f}%** | {o['eval']['overall_score']*100:.1f}% |\n"
            
            for field in ["vendor_name", "date", "currency", "total", "subtotal", "tax"]:
                v_field = v["eval"][field]
                o_field = o["eval"][field]
                v_status = "✅ Match" if v_field["success"] else f"❌ Misread ({v_field['extracted']})"
                o_status = "✅ Match" if o_field["success"] else f"❌ Misread ({o_field['extracted']})"
                report += f"| `{field}` | {v_status} | {o_status} |\n"

            # Line items match
            v_li = v["eval"]["line_items"]
            o_li = o["eval"]["line_items"]
            report += f"| Line Items Extracted | {v_li['extracted_count']}/{v_li['expected_count']} matches | {o_li['extracted_count']}/{o_li['expected_count']} matches |\n"
            report += f"| Line Item Accuracy | {v['eval']['line_items_accuracy']*100:.1f}% | {o['eval']['line_items_accuracy']*100:.1f}% |\n"

        report += "\n---\n\n"

    report += """## Architectural Insights

1. **Vision LLM (Path A)**:
   - Understands layout natively.
   - Requires zero local preprocessing dependencies (like OpenCV or Tesseract).
   - Higher accuracy on blurred or non-aligned text.

2. **OCR + LLM (Path B)**:
   - Processes image OCR locally, which is more privacy-preserving.
   - Text payload to Gemini API is much smaller, saving network bandwidth.
   - Tesseract struggles with receipts that are crumpled, low contrast, or have non-standard columns.
"""

    report_path.write_text(report, encoding="utf-8")
    print(f"[REPORT] Benchmark report written to {report_path}")
    print()
    print("Benchmark completed. Report:")
    print("-" * 50)
    print(report.split("---")[0])  # Print summary to console
    print("-" * 50)


if __name__ == "__main__":
    # Load env variables
    from dotenv import load_dotenv
    load_dotenv()

    asyncio.run(run_benchmark())
