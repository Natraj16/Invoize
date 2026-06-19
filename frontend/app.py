"""
Gradio UI for Invoize.

Why Gradio instead of React/Next.js?
- 10x faster to build for a portfolio project (minutes, not days)
- Built-in file upload, JSON display, and download components
- Python-native — no JS build toolchain, no npm, no webpack
- Still looks professional enough for a demo
- Lets us focus time on the DIFFERENTIATOR (validation layer)
  instead of frontend plumbing

The UI calls the FastAPI backend over HTTP, keeping frontend and backend
cleanly separated — same architecture as a "real" app, just with Gradio
as the presentation layer instead of React.
"""

import json
import time
import os
import httpx
import gradio as gr
import io
from PIL import Image

API_BASE = os.getenv("API_BASE", f"http://127.0.0.1:{os.getenv('PORT', '8000')}")


def preview_file(file):
    """Generate a PIL Image preview of an uploaded image or the first page of a PDF."""
    if not file:
        return None
    try:
        # Resolve actual file path from string, object, or dict
        if isinstance(file, str):
            file_path = file
        elif hasattr(file, "name"):
            file_path = file.name
        elif isinstance(file, dict) and "name" in file:
            file_path = file["name"]
        else:
            file_path = str(file)

        if not file_path or not os.path.exists(file_path):
            return None

        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        if ext == "pdf":
            import fitz
            doc = fitz.open(file_path)
            if doc.page_count > 0:
                page = doc[0]
                # Render page to a pixmap (DPI 150 is plenty for local preview)
                pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72.0, 150 / 72.0))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                doc.close()
                return img
            doc.close()
            return None
        else:
            return file_path
    except Exception as e:
        print(f"Error rendering preview: {e}")
        return None


def extract_receipt(file, method="vision_llm") -> tuple[str, str, str, str]:
    """
    Upload a file to the FastAPI backend and return the results.

    Returns a tuple of (formatted_view, status_message, raw_json, validation_info)
    for the four output components in the UI.
    """
    if file is None:
        return "", "Please upload a receipt image or PDF.", "", ""

    start = time.time()

    try:
        # Determine MIME type from file extension
        filename = file.name if hasattr(file, "name") else "upload"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        mime_map = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "pdf": "application/pdf",
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

        # Read file bytes
        if hasattr(file, "read"):
            file_bytes = file.read()
        else:
            with open(file, "rb") as f:
                file_bytes = f.read()

        # Upload to FastAPI backend
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{API_BASE}/upload",
                params={"method": method},
                files={"file": (filename.split("/")[-1].split("\\")[-1], file_bytes, mime_type)},
            )

        elapsed = time.time() - start
        data = response.json()

        if data.get("success"):
            receipt = data["data"]

            # Format a human-readable summary
            summary_lines = [
                f"## {receipt['vendor_name']}",
                "",
            ]
            if receipt.get("vendor_address"):
                summary_lines.append(f"**Address:** {receipt['vendor_address']}")
            if receipt.get("date"):
                summary_lines.append(f"**Date:** {receipt['date']}")
            if receipt.get("time"):
                summary_lines.append(f"**Time:** {receipt['time']}")
            summary_lines.append(f"**Currency:** {receipt['currency']}")
            summary_lines.append("")
            summary_lines.append("### Items")
            summary_lines.append("| Item | Qty | Unit Price | Total |")
            summary_lines.append("|------|-----|-----------|-------|")
            for item in receipt.get("line_items", []):
                summary_lines.append(
                    f"| {item['name']} | {item['quantity']} | "
                    f"${item['unit_price']:.2f} | ${item['total_price']:.2f} |"
                )
            summary_lines.append("")
            if receipt.get("subtotal") is not None:
                summary_lines.append(f"**Subtotal:** ${receipt['subtotal']:.2f}")
            if receipt.get("tax") is not None:
                summary_lines.append(f"**Tax:** ${receipt['tax']:.2f}")
            if receipt.get("tip") is not None:
                summary_lines.append(f"**Tip:** ${receipt['tip']:.2f}")
            summary_lines.append(f"### Total: ${receipt['total']:.2f}")
            if receipt.get("payment_method"):
                summary_lines.append(f"**Payment:** {receipt['payment_method']}")

            summary = "\n".join(summary_lines)

            status = (
                f"Extraction successful in {elapsed:.1f}s "
                f"(API: {data.get('processing_time_seconds', '?')}s)"
            )

            # Editable JSON
            editable_json = json.dumps(receipt, indent=2)

            # Format validation results
            validation_md = _format_validation(data.get("validation"))

            return summary, status, editable_json, validation_md
        else:
            error_msg = data.get("error", "Unknown error")
            return "", f"Extraction failed: {error_msg}", "", ""

    except httpx.ConnectError:
        return (
            "",
            "Cannot connect to API server. Make sure the FastAPI backend is running on port 8000.",
            "",
            "",
        )
    except Exception as e:
        return "", f"Error: {type(e).__name__}: {str(e)}", "", ""


def save_edited_json(json_text: str) -> str:
    """Validate that the edited JSON is still valid."""
    if not json_text.strip():
        return "No data to validate."
    try:
        data = json.loads(json_text)
        return f"JSON is valid. {len(data)} top-level fields."
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"


def export_json(json_text: str) -> str | None:
    """Export the current JSON to a downloadable file."""
    if not json_text.strip():
        return None
    try:
        # Validate
        data = json.loads(json_text)
        # Write to temp file
        import tempfile
        import os
        
        vendor = data.get("vendor_name", "receipt").replace(" ", "_")[:20]
        date = data.get("date", "unknown")
        fname = f"{vendor}_{date}.json"
        
        path = os.path.join(tempfile.gettempdir(), fname)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path
    except Exception:
        return None


def export_csv(json_text: str) -> str | None:
    """Export line items as CSV."""
    if not json_text.strip():
        return None
    try:
        import csv
        import tempfile
        import os
        
        data = json.loads(json_text)
        vendor = data.get("vendor_name", "receipt").replace(" ", "_")[:20]
        date = data.get("date", "unknown")
        fname = f"{vendor}_{date}.csv"
        
        path = os.path.join(tempfile.gettempdir(), fname)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            # Header row with receipt metadata
            writer.writerow(["Vendor", "Date", "Currency", "Subtotal", "Tax", "Total"])
            writer.writerow([
                data.get("vendor_name", ""),
                data.get("date", ""),
                data.get("currency", ""),
                data.get("subtotal", ""),
                data.get("tax", ""),
                data.get("total", ""),
            ])
            writer.writerow([])
            # Line items
            writer.writerow(["Item", "Quantity", "Unit Price", "Total Price"])
            for item in data.get("line_items", []):
                writer.writerow([
                    item.get("name", ""),
                    item.get("quantity", ""),
                    item.get("unit_price", ""),
                    item.get("total_price", ""),
                ])
        return path
    except Exception:
        return None


def _format_validation(validation: dict | None) -> str:
    """Format validation results as readable HTML/markdown."""
    if not validation:
        return "<p style='color: var(--color-muted-ash); font-style: italic;'>No validation data available.</p>"

    lines = []

    # Confidence badge
    confidence = validation.get("overall_confidence", "unknown")
    color_map = {
        "high": ("rgba(16, 185, 129, 0.15)", "#10b981", "HIGH"),
        "medium": ("rgba(245, 158, 11, 0.15)", "#f59e0b", "MEDIUM"),
        "low": ("rgba(239, 68, 68, 0.15)", "#ef4444", "LOW")
    }
    bg, fg, label = color_map.get(confidence, ("rgba(156, 163, 175, 0.15)", "#9ca3af", "UNKNOWN"))
    badge = f'<span style="background-color: {bg}; color: {fg}; padding: 4px 8px; border-radius: 4px; font-weight: 500; font-size: 13px; display: inline-block; letter-spacing: 0.05em; text-transform: uppercase;">{label} CONFIDENCE</span>'
    
    lines.append(f"### Overall Status: {badge}")
    lines.append("")

    # Key metrics
    math_status = "<span style='color: #10b981; font-weight: 500;'>VALID</span>" if validation.get('math_valid') else "<span style='color: #ef4444; font-weight: 500;'>INVALID</span>"
    review_status = "<span style='color: #ef4444; font-weight: 500;'>YES</span>" if validation.get('needs_manual_review') else "<span style='color: #10b981; font-weight: 500;'>NO</span>"
    
    lines.append(f"- **Math Checks:** {math_status}")
    lines.append(f"- **Completeness Score:** {validation.get('completeness_score', 0):.0%}")
    lines.append(f"- **Needs Manual Review:** {review_status}")
    lines.append("")

    # Flags
    flags = validation.get("flags", [])
    if flags:
        lines.append("### Pipeline Flags")
        severity_map = {
            "error": ("rgba(239, 68, 68, 0.15)", "#ef4444", "ERROR"),
            "warning": ("rgba(245, 158, 11, 0.15)", "#f59e0b", "WARN"),
            "info": ("rgba(59, 130, 246, 0.15)", "#3b82f6", "INFO")
        }
        for flag in flags:
            sev = flag.get("severity", "info")
            s_bg, s_fg, s_lbl = severity_map.get(sev, ("rgba(156, 163, 175, 0.15)", "#9ca3af", "INFO"))
            icon = f'<span style="background-color: {s_bg}; color: {s_fg}; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 500; display: inline-block; margin-right: 6px; letter-spacing: 0.02em;">{s_lbl}</span>'
            field = flag.get("field_name", "")
            msg = flag.get("message", "")
            lines.append(f"- {icon} **{field}**: {msg}")
    else:
        lines.append("<p style='color: #10b981; font-weight: 500; margin-top: 8px;'>✓ No issues found. All validation checks passed.</p>")

    return "\n".join(lines)


# --- Build the Gradio Interface ---

custom_css = """
@import url('https://fonts.googleapis.com/css2?family=Archivo+Black&family=Inter:wght@300;400;450;900&display=swap');

:root {
  --color-cream-paper: #ffedd2;
  --color-void-black: #0d0d0d;
  --color-polished-white: #ffffff;
  --color-hairline-gray: #e5e7eb;
  --color-muted-ash: #9e9e9e;
  --color-surface-charcoal: #1f1f1f;
  
  --font-telka: 'Inter', sans-serif;
  --font-telkaextended: 'Archivo Black', sans-serif;
}

body, html, .gradio-container, .main, .gradio-container > div {
    background-color: var(--color-void-black) !important;
    margin: 0 !important;
    padding: 0 !important;
    font-family: var(--font-telka) !important;
    font-weight: 300 !important;
    max-width: 100% !important;
    border: none !important;
}

#main-container {
    display: flex !important;
    flex-direction: row !important;
    min-height: 100vh !important;
    margin: 0 !important;
    padding: 0 !important;
    gap: 0 !important;
    width: 100% !important;
}

@media (max-width: 768px) {
    #main-container {
        flex-direction: column !important;
    }
    #cream-panel, #void-panel {
        min-height: auto !important;
        height: auto !important;
        padding: 24px !important;
    }
}

#cream-panel {
    background-color: var(--color-cream-paper) !important;
    min-height: 100vh !important;
    padding: 48px !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: space-between !important;
    gap: 32px !important;
    border-right: 1px solid var(--color-hairline-gray) !important;
    flex: 1 !important;
}

#void-panel {
    background-color: var(--color-void-black) !important;
    min-height: 100vh !important;
    padding: 48px !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: space-between !important;
    gap: 32px !important;
    flex: 1 !important;
    overflow-y: auto !important;
}

/* Typography elements */
.brand-wordmark {
    font-family: var(--font-telka) !important;
    font-weight: 400 !important;
    font-size: 24px !important;
    color: var(--color-void-black) !important;
    text-align: center !important;
    letter-spacing: -0.02em !important;
    margin-bottom: 8px !important;
}

.telka-display {
    font-family: var(--font-telkaextended) !important;
    font-weight: 900 !important;
    font-size: 32px !important;
    line-height: 1.13 !important;
    letter-spacing: 0.01em !important;
    text-transform: uppercase !important;
    color: var(--color-polished-white) !important;
    margin-bottom: 24px !important;
}

.telka-text {
    font-family: var(--font-telka) !important;
    font-weight: 300 !important;
    letter-spacing: -0.02em !important;
    font-size: 16px !important;
    line-height: 1.5 !important;
    color: var(--color-void-black) !important;
}

.tagline {
    font-family: var(--font-telka) !important;
    font-weight: 300 !important;
    font-size: 12px !important;
    color: var(--color-void-black) !important;
    opacity: 0.6 !important;
    text-align: center !important;
    letter-spacing: -0.02em !important;
}

/* Left panel content wrapper styling */
#cream-panel * {
    color: var(--color-void-black) !important;
}

/* Dropzone and video preview visual box */
.video-preview-tile {
    background-color: var(--color-cream-paper) !important;
    border: 1px solid var(--color-hairline-gray) !important;
    border-radius: 10px !important;
    padding: 16px !important;
    box-shadow: none !important;
}

/* Form component overrides for dark panel */
.void-input {
    background-color: var(--color-surface-charcoal) !important;
    border: 1px solid rgba(229, 230, 235, 0.1) !important;
    border-radius: 6px !important;
    padding: 8px !important;
    box-shadow: none !important;
}

.void-input label, .void-input span, .void-input input, .void-input textarea {
    color: var(--color-polished-white) !important;
    font-family: var(--font-telka) !important;
}

.void-input textarea, .void-input input {
    background-color: transparent !important;
    border: none !important;
}

/* Radio button text coloring */
.void-input input[type="radio"]:checked + span {
    color: var(--color-cream-paper) !important;
    font-weight: 450 !important;
}

/* Button variants */
.btn-filled-white {
    background-color: var(--color-polished-white) !important;
    color: var(--color-void-black) !important;
    font-family: var(--font-telka) !important;
    font-weight: 450 !important;
    font-size: 14px !important;
    border-radius: 6px !important;
    border: none !important;
    padding: 12px 16px !important;
    cursor: pointer !important;
    box-shadow: none !important;
    transition: background-color 0.2s ease !important;
    text-align: center !important;
    display: block !important;
    width: 100% !important;
}
.btn-filled-white:hover {
    background-color: #f0f0f0 !important;
}

.btn-outlined-dark {
    background-color: transparent !important;
    color: var(--color-polished-white) !important;
    font-family: var(--font-telka) !important;
    font-weight: 450 !important;
    font-size: 14px !important;
    border-radius: 6px !important;
    border: 1px solid rgba(229, 230, 235, 0.3) !important;
    padding: 12px 16px !important;
    cursor: pointer !important;
    box-shadow: none !important;
    transition: background-color 0.2s ease, border-color 0.2s ease !important;
    text-align: center !important;
    display: block !important;
    width: 100% !important;
}
.btn-outlined-dark:hover {
    background-color: var(--color-surface-charcoal) !important;
    border-color: rgba(229, 230, 235, 0.6) !important;
}

/* Bare tabs */
.void-tabs {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
}

.void-tabs .tab-nav {
    border-bottom: 1px solid rgba(229, 230, 235, 0.1) !important;
    background: transparent !important;
    gap: 16px !important;
    box-shadow: none !important;
}

.void-tabs .tab-nav button {
    color: var(--color-muted-ash) !important;
    font-family: var(--font-telka) !important;
    font-weight: 450 !important;
    border: none !important;
    background: transparent !important;
    font-size: 14px !important;
    padding: 8px 0 !important;
    border-radius: 0 !important;
    box-shadow: none !important;
}

.void-tabs .tab-nav button.selected {
    color: var(--color-polished-white) !important;
    border-bottom: 2px solid var(--color-polished-white) !important;
}

.void-tabs .tabitem {
    border: none !important;
    background: transparent !important;
}

.void-tabs .tabitem * {
    color: var(--color-polished-white) !important;
}

/* Footer layout elements */
.footer-row {
    display: flex !important;
    gap: 20px !important;
    justify-content: flex-start !important;
    margin-top: 16px !important;
}

.footer-link {
    color: var(--color-muted-ash) !important;
    font-family: var(--font-telka) !important;
    font-size: 12px !important;
    font-weight: 300 !important;
    text-decoration: none !important;
    transition: color 0.2s ease !important;
}

.footer-link:hover {
    color: var(--color-hairline-gray) !important;
}

.legal-text {
    font-family: var(--font-telka) !important;
    font-size: 12px !important;
    font-weight: 300 !important;
    color: var(--color-muted-ash) !important;
    line-height: 1.5 !important;
    margin-top: 16px !important;
}

.legal-text a {
    color: var(--color-hairline-gray) !important;
    text-decoration: underline !important;
}
"""

# --- Build the Gradio Interface ---

custom_css = """
@import url('https://fonts.googleapis.com/css2?family=Fira+Code&family=Inter:ital,opsz,wght@0,14..32,100..900;1,14..32,100..900&display=swap');

:root {
  /* Colors */
  --color-onyx: #08090a;
  --color-charcoal: #0f1011;
  --color-obsidian: #161718;
  --color-graphite: #23252a;
  --color-iron: #323334;
  --color-steel: #383b3f;
  --color-slate: #62666d;
  --color-fog: #8a8f98;
  --color-mist: #d0d6e0;
  --color-platinum: #e5e5e6;
  --color-snow: #f7f8f8;
  --color-acid-lime: #e4f222;
  --color-indigo: #5e6ad2;
  --color-emerald: #27a644;
  --color-crimson: #eb5757;
  --color-cyan: #02b8cc;

  /* Typography */
  --font-inter: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif;
  --font-mono: 'Fira Code', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;

  /* Shadows */
  --shadow-sm: rgba(0, 0, 0, 0.4) 0px 2px 4px 0px;
  --shadow-xl: rgba(8, 9, 10, 0.6) 0px 4px 32px 0px;
  --shadow-subtle: rgb(35, 37, 42) 0px 0px 0px 1px inset;
}

footer {
    display: none !important;
}

body, html, .gradio-container, .main, .gradio-container > div {
    background-color: var(--color-onyx) !important;
    margin: 0 !important;
    padding: 0 !important;
    font-family: var(--font-inter) !important;
    font-weight: 300 !important;
    max-width: 100% !important;
    border: none !important;
    color: var(--color-snow) !important;
}

#main-container {
    display: flex !important;
    flex-direction: row !important;
    min-height: 100vh !important;
    margin: 0 !important;
    padding: 0 !important;
    gap: 0 !important;
    width: 100% !important;
}

@media (max-width: 768px) {
    #main-container {
        flex-direction: column !important;
    }
    #left-panel, #right-panel {
        min-height: auto !important;
        height: auto !important;
        padding: 32px 24px !important;
    }
}

#left-panel {
    background-color: var(--color-charcoal) !important;
    min-height: 100vh !important;
    padding: 48px !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-start !important;
    gap: 24px !important;
    border-right: 1px solid var(--color-graphite) !important;
    flex: 1 !important;
}

#right-panel {
    background-color: var(--color-obsidian) !important;
    min-height: 100vh !important;
    padding: 48px !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: space-between !important;
    gap: 32px !important;
    flex: 1 !important;
    overflow-y: auto !important;
}

/* Typography styles */
.brand-title {
    font-family: var(--font-mono) !important;
    font-size: 24px !important;
    font-weight: 500 !important;
    color: var(--color-acid-lime) !important;
    letter-spacing: -0.05em !important;
    margin-bottom: 8px !important;
}

.engine-title {
    font-family: var(--font-inter) !important;
    font-weight: 900 !important;
    font-size: 32px !important;
    letter-spacing: -0.03em !important;
    text-transform: uppercase !important;
    color: var(--color-snow) !important;
    margin-bottom: 24px !important;
}

.sub-text {
    font-size: 14px !important;
    line-height: 1.6 !important;
    color: var(--color-mist) !important;
}

/* Document cards (File Upload & PDF Preview) */
.panel-card {
    background-color: var(--color-onyx) !important;
    border: 1px solid var(--color-graphite) !important;
    border-radius: var(--radius-xl) !important;
    padding: 16px !important;
    box-shadow: var(--shadow-sm) !important;
}

.preview-box {
    background-color: var(--color-onyx) !important;
    border: 1px solid var(--color-graphite) !important;
    border-radius: var(--radius-xl) !important;
    overflow: hidden !important;
    box-shadow: var(--shadow-xl) !important;
    margin-top: 16px !important;
}

/* Inputs on right panel */
.dark-input {
    background-color: var(--color-charcoal) !important;
    border: 1px solid var(--color-graphite) !important;
    border-radius: var(--radius-md) !important;
    padding: 12px !important;
    box-shadow: var(--shadow-md) !important;
}

.dark-input label, .dark-input span, .dark-input input, .dark-input textarea {
    color: var(--color-snow) !important;
    font-family: var(--font-inter) !important;
}

.dark-input textarea, .dark-input input {
    background-color: transparent !important;
    border: none !important;
}

.dark-input input[type="radio"]:checked + span {
    color: var(--color-acid-lime) !important;
    font-weight: 600 !important;
}

/* Buttons */
.btn-primary {
    background-color: var(--color-acid-lime) !important;
    color: var(--color-onyx) !important;
    font-family: var(--font-inter) !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    border-radius: var(--radius-md) !important;
    border: none !important;
    padding: 14px 20px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    width: 100% !important;
    text-transform: uppercase !important;
    letter-spacing: 0.05em !important;
}

.btn-primary:hover {
    background-color: #d4e015 !important;
    transform: translateY(-1px) !important;
}

.btn-secondary {
    background-color: transparent !important;
    color: var(--color-snow) !important;
    font-family: var(--font-inter) !important;
    font-weight: 500 !important;
    font-size: 13px !important;
    border-radius: var(--radius-md) !important;
    border: 1px solid var(--color-steel) !important;
    padding: 12px 18px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    width: 100% !important;
    text-transform: uppercase !important;
    letter-spacing: 0.05em !important;
}

.btn-secondary:hover {
    background-color: var(--color-graphite) !important;
    border-color: var(--color-acid-lime) !important;
    transform: translateY(-1px) !important;
}

/* Dark Tabs */
.dark-tabs {
    border: none !important;
    background: transparent !important;
}

.dark-tabs .tab-nav {
    border-bottom: 1px solid var(--color-graphite) !important;
    background: transparent !important;
    gap: 20px !important;
}

.dark-tabs .tab-nav button {
    color: var(--color-fog) !important;
    font-family: var(--font-inter) !important;
    font-weight: 500 !important;
    border: none !important;
    background: transparent !important;
    font-size: 14px !important;
    padding: 8px 0 !important;
    border-radius: 0 !important;
}

.dark-tabs .tab-nav button:hover {
    color: var(--color-snow) !important;
}

.dark-tabs .tab-nav button.selected {
    color: var(--color-snow) !important;
    border-bottom: 2px solid var(--color-acid-lime) !important;
}

.dark-tabs .tabitem {
    border: none !important;
    background: transparent !important;
    padding: 12px 0 !important;
}

/* Status logs label */
.status-container {
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
    margin-bottom: 6px !important;
}

.status-dot-green {
    width: 6px !important;
    height: 6px !important;
    background-color: var(--color-emerald) !important;
    border-radius: 50% !important;
    display: inline-block !important;
    box-shadow: 0 0 6px var(--color-emerald) !important;
}

.status-label {
    font-size: 12px !important;
    font-weight: 500 !important;
    letter-spacing: 0.05em !important;
    color: var(--color-fog) !important;
}

/* Footer elements */
.footer-row {
    display: flex !important;
    gap: 20px !important;
    justify-content: flex-start !important;
    margin-top: 16px !important;
}

.footer-link {
    color: var(--color-fog) !important;
    font-family: var(--font-inter) !important;
    font-size: 12px !important;
    font-weight: 300 !important;
    text-decoration: none !important;
    transition: color 0.2s ease !important;
}

.footer-link:hover {
    color: var(--color-acid-lime) !important;
}

.legal-text {
    font-family: var(--font-inter) !important;
    font-size: 12px !important;
    font-weight: 300 !important;
    color: var(--color-fog) !important;
    line-height: 1.5 !important;
    margin-top: 16px !important;
}

.legal-text a {
    color: var(--color-mist) !important;
    text-decoration: underline !important;
}
"""

with gr.Blocks(
    title="Invoize",
    css=custom_css,
) as demo:
    demo.css = custom_css
    with gr.Row(equal_height=True, elem_id="main-container"):
        # Left Column: Upload panel + Live Image/PDF Preview
        with gr.Column(scale=1, elem_id="left-panel"):
            with gr.Column():
                gr.HTML('<div class="brand-title">Invoize</div>')
                
                # File Input Area
                file_input = gr.File(
                    label="Drop receipt or invoice here",
                    file_types=[".jpg", ".jpeg", ".png", ".webp", ".pdf"],
                    type="filepath",
                    elem_classes=["panel-card"]
                )
                
                # Live Preview Image Component (Visible Uploaded File)
                preview_image = gr.Image(
                    label="DOCUMENT PREVIEW",
                    interactive=False,
                    elem_classes=["preview-box"]
                )
            


        # Right Column: UI action panel + structured output
        with gr.Column(scale=1, elem_id="right-panel"):
            with gr.Column():
                method_input = gr.State("vision_llm")
                
                extract_btn = gr.Button(
                    "EXTRACT STRUCTURED DATA",
                    elem_classes=["btn-primary"]
                )
                
                with gr.Column():
                    gr.HTML(
                        '<div class="status-container">'
                        '<span class="status-dot-green"></span>'
                        '<span class="status-label">PIPELINE STATUS LOGS</span>'
                        '</div>'
                    )
                    status_output = gr.Textbox(
                        show_label=False,
                        value="Ready for document upload.",
                        interactive=False,
                        elem_classes=["dark-input"]
                    )

            # Outputs Tabs
            with gr.Tabs(elem_classes=["dark-tabs"]):
                with gr.TabItem("Formatted View"):
                    formatted_output = gr.Markdown(
                        value="*Upload document to display extracted fields.*"
                    )

                with gr.TabItem("Confidence & Flags"):
                    validation_output = gr.Markdown(
                        value="*Independent math checks and plausibility flags compile here.*"
                    )

                with gr.TabItem("Source JSON"):
                    json_editor = gr.Code(
                        label="SOURCE CODE (EDITABLE)",
                        language="json",
                        lines=15,
                        elem_classes=["dark-input"]
                    )
                    with gr.Row():
                        validate_btn = gr.Button("VALIDATE SCHEMA", elem_classes=["btn-secondary"])
                    validate_output = gr.Textbox(
                        label="SCHEMA VALIDATION RESULTS",
                        interactive=False,
                        elem_classes=["dark-input"]
                    )

            # Downloads & Navigation footer
            with gr.Column():
                with gr.Row():
                    export_json_btn = gr.Button("DOWNLOAD JSON", elem_classes=["btn-secondary"])
                    export_csv_btn = gr.Button("DOWNLOAD CSV", elem_classes=["btn-secondary"])
                
                json_file = gr.File(label="JSON Download", visible=False, elem_classes=["dark-input"])
                csv_file = gr.File(label="CSV Download", visible=False, elem_classes=["dark-input"])

                gr.HTML(
                    '<div class="legal-text">'
                    'By submitting documents, you agree to our '
                    '<a href="#">Terms of Service</a>, '
                    '<a href="#">Acceptable Use Policy</a>, and '
                    '<a href="#">Privacy Policy</a>.'
                    '</div>'
                )

                gr.HTML(
                    '<div class="footer-row">'
                    '<a class="footer-link" href="https://github.com" target="_blank">GitHub</a>'
                    '<a class="footer-link" href="#" target="_blank">Docs</a>'
                    '</div>'
                )

    # --- Event Handlers ---
    
    # Auto-generate live preview on file upload/change
    file_input.change(
        fn=preview_file,
        inputs=[file_input],
        outputs=[preview_image],
    )

    extract_btn.click(
        fn=extract_receipt,
        inputs=[file_input, method_input],
        outputs=[formatted_output, status_output, json_editor, validation_output],
    )

    validate_btn.click(
        fn=save_edited_json,
        inputs=[json_editor],
        outputs=[validate_output],
    )

    export_json_btn.click(
        fn=export_json,
        inputs=[json_editor],
        outputs=[json_file],
    )

    export_csv_btn.click(
        fn=export_csv,
        inputs=[json_editor],
        outputs=[csv_file],
    )


if __name__ == "__main__":
    port = int(os.getenv("GRADIO_SERVER_PORT", 7860))
    demo.launch(server_port=port)


