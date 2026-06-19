# Invoize — Benchmarking Results

This report evaluates and compares **Path A (Vision LLM)** and **Path B (OCR + LLM)** extraction pipelines.

## Benchmark Summary

| Pipeline | Success Rate | Average Accuracy | Average Processing Time |
|----------|--------------|------------------|-------------------------|
| **Path A: Vision LLM** (Gemini 2.5 Flash) | 1/1 (100.0%) | **100.0%** | **8.89 seconds** |
| **Path B: OCR + LLM** (Tesseract + Gemini) | 1/1 (100.0%) | 41.7% | 4.56 seconds |

---

## Detailed Performance by File

### File: `receipt_1.png`

| Metric                 | Path A: Vision LLM  | Path B: OCR + LLM           |
| :--------------------- | :------------------ | :-------------------------- |
| Extraction Status      | ✅ Success          | ✅ Success                  |
| Processing Time        | 8.89s               | 4.56s                       |
| Overall Accuracy Score | **100.0%**          | 41.7%                       |
| `vendor_name`          | ✅ Match            | ❌ Misread (GROCERY STORE)  |
| `date`                 | ✅ Match            | ✅ Match                    |
| `currency`             | ✅ Match            | ✅ Match                    |
| `total`                | ✅ Match            | ❌ Misread (9.47)           |
| `subtotal`             | ✅ Match            | ❌ Misread (None)           |
| `tax`                  | ✅ Match            | ❌ Misread (None)           |
| Line Items Extracted   | 4/4 matches         | 3/4 matches                 |
| Line Item Accuracy     | 100.0%              | 50.0%                       |

---

## Architectural Insights

1. **Vision LLM (Path A)**:
   - Understands layout natively.
   - Requires zero local preprocessing dependencies (like OpenCV or Tesseract).
   - Higher accuracy on blurred or non-aligned text.

2. **OCR + LLM (Path B)**:
   - Processes image OCR locally, which is more privacy-preserving.
   - Text payload to Gemini API is much smaller, saving network bandwidth.
   - Tesseract struggles with receipts that are crumpled, low contrast, or have non-standard columns.
