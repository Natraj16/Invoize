"""
Deterministic text parser for Invoize.

Parses CSV and JSON receipt/invoice files directly in Python, bypassing the Gemini API
to save quota and ensure instant response times.
"""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from app.schemas import ReceiptData, LineItem

def parse_float(val: Any) -> Optional[float]:
    """Parse float from any type, cleaning currency characters."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    # Remove currency symbols and formatting (keep digits, dot, minus)
    cleaned = re.sub(r"[^\d\.\-]", "", str(val))
    if not cleaned or cleaned == "." or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def parse_date_string(val: Any) -> Optional[str]:
    """Normalize date strings to YYYY-MM-DD format."""
    if not val:
        return None
    val_str = str(val).strip()
    
    # Common format mappings
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(val_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # Regex extract YYYY-MM-DD
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", val_str)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        
    match = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", val_str)
    if match:
        p1, p2, year = int(match.group(1)), int(match.group(2)), match.group(3)
        if p1 > 12:  # Must be DD/MM/YYYY
            return f"{year}-{p2:02d}-{p1:02d}"
        else:  # Assume MM/DD/YYYY
            return f"{year}-{p1:02d}-{p2:02d}"
            
    return val_str

def clean_currency(val: Any) -> str:
    """Standardize currency to ISO 4217 code. Default to INR."""
    if not val:
        return "INR"
    val_str = str(val).strip().upper()
    if val_str in ("INR", "RS", "₹"):
        return "INR"
    if val_str in ("USD", "$"):
        return "USD"
    if val_str in ("EUR", "€"):
        return "EUR"
    if val_str in ("GBP", "£"):
        return "GBP"
        
    if "₹" in val_str or "RS" in val_str:
        return "INR"
    if "$" in val_str:
        return "USD"
    if "€" in val_str:
        return "EUR"
    if "£" in val_str:
        return "GBP"
        
    # Check for valid 3-letter code
    if len(val_str) == 3 and val_str.isalpha():
        return val_str
    return "INR"

def map_dict_to_receipt(d: dict, default_vendor: str = "Unknown Store") -> ReceiptData:
    """Map a generic Python dictionary to ReceiptData schema."""
    
    # 1. Vendor Name
    vendor_name = None
    for k in ("vendor_name", "vendor", "store", "merchant", "merchant_name", "seller"):
        if k in d and d[k]:
            vendor_name = str(d[k]).strip()
            break
    if not vendor_name:
        vendor_name = default_vendor

    # 2. Address
    vendor_address = None
    for k in ("vendor_address", "address", "location", "store_address"):
        if k in d and d[k]:
            vendor_address = str(d[k]).strip()
            break

    # 3. Date
    date_val = None
    for k in ("date", "receipt_date", "invoice_date", "created_at", "transaction_date"):
        if k in d and d[k]:
            date_val = parse_date_string(d[k])
            break

    # 4. Time
    time_val = None
    for k in ("time", "receipt_time", "invoice_time", "transaction_time"):
        if k in d and d[k]:
            time_val = str(d[k]).strip()
            break

    # 5. Currency
    currency_val = "INR"
    for k in ("currency", "currency_code", "currency_symbol", "symbol"):
        if k in d and d[k]:
            currency_val = clean_currency(d[k])
            break

    # 6. Payment Method
    payment_method = None
    for k in ("payment_method", "payment", "pay_method", "mode", "payment_mode"):
        if k in d and d[k]:
            payment_method = str(d[k]).strip()
            break

    # 7. Math totals
    subtotal = None
    for k in ("subtotal", "sub_total", "net_amount"):
        if k in d and d[k] is not None:
            subtotal = parse_float(d[k])
            break

    discount = 0.0
    for k in ("discount", "discount_amount", "discounts", "savings"):
        if k in d and d[k] is not None:
            discount = parse_float(d[k]) or 0.0
            break

    tax = None
    for k in ("tax", "tax_amount", "vat", "gst", "cgst", "sgst", "igst"):
        if k in d and d[k] is not None:
            tax = parse_float(d[k])
            break

    tip = None
    for k in ("tip", "gratuity", "service_charge"):
        if k in d and d[k] is not None:
            tip = parse_float(d[k])
            break

    total = None
    for k in ("total", "total_amount", "grand_total", "amount_due", "amount"):
        if k in d and d[k] is not None:
            total = parse_float(d[k])
            break

    # 8. Line Items
    line_items: list[LineItem] = []
    items_list = None
    for k in ("line_items", "items", "products", "menu_items", "details", "lines"):
        if k in d and isinstance(d[k], list):
            items_list = d[k]
            break

    if items_list:
        for item in items_list:
            if not isinstance(item, dict):
                continue
            
            # Find item name
            item_name = None
            for k in ("name", "description", "item", "title", "product_name", "product"):
                if k in item and item[k]:
                    item_name = str(item[k]).strip()
                    break
            if not item_name:
                continue

            # Quantity
            qty = 1.0
            for k in ("quantity", "qty", "count", "units"):
                if k in item and item[k] is not None:
                    qty = parse_float(item[k]) or 1.0
                    break

            # Unit Price & Total Price
            u_price = None
            for k in ("unit_price", "price", "rate", "each", "unit_rate"):
                if k in item and item[k] is not None:
                    u_price = parse_float(item[k])
                    break

            t_price = None
            for k in ("total_price", "total", "amount", "line_total"):
                if k in item and item[k] is not None:
                    t_price = parse_float(item[k])
                    break

            # Infer prices if one is missing
            if u_price is None and t_price is not None:
                u_price = round(t_price / qty, 2)
            elif u_price is not None and t_price is None:
                t_price = round(qty * u_price, 2)
            elif u_price is None and t_price is None:
                u_price = 0.0
                t_price = 0.0

            line_items.append(
                LineItem(
                    name=item_name,
                    quantity=qty,
                    unit_price=u_price,
                    total_price=t_price
                )
            )

    # Infer total if missing
    if total is None:
        calc_total = sum(item.total_price for item in line_items)
        if subtotal is not None:
            calc_total = subtotal
        if tax is not None:
            calc_total += tax
        if tip is not None:
            calc_total += tip
        calc_total -= discount
        total = round(calc_total, 2)

    return ReceiptData(
        vendor_name=vendor_name,
        vendor_address=vendor_address,
        date=date_val,
        time=time_val,
        currency=currency_val,
        line_items=line_items,
        subtotal=subtotal,
        discount=discount,
        tax=tax,
        tip=tip,
        total=total,
        payment_method=payment_method
    )

def parse_csv_content(text_content: str, filename: str) -> Optional[ReceiptData]:
    """
    Parse CSV receipt content. Supports flat Invoize exports or structured tables.
    """
    lines = [line for line in text_content.splitlines() if line.strip()]
    if not lines:
        return None

    # Use csv.reader
    reader = csv.reader(lines)
    rows = list(reader)
    if not rows:
        return None

    # Detect header row
    header_idx = -1
    for idx, row in enumerate(rows):
        # Look for headers containing typical item columns
        row_cleaned = [str(cell).strip().lower() for cell in row]
        if any(h in row_cleaned for h in ("item name", "item", "description", "product", "item_name")):
            header_idx = idx
            break

    # If no standard header found, check if it's a simple metadata key-value CSV
    if header_idx == -1:
        # Check if rows are structured like Key, Value
        metadata = {}
        for row in rows:
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                k = row[0].strip().lower().replace(" ", "_")
                metadata[k] = row[1].strip()
        # If we found at least total or vendor
        if any(k in metadata for k in ("vendor", "vendor_name", "total", "grand_total")):
            return map_dict_to_receipt(metadata, default_vendor=Path(filename).stem)
        return None

    # Tabular CSV processing
    headers = [str(h).strip().lower() for h in rows[header_idx]]
    data_rows = rows[header_idx+1:]

    # Map headers to indices
    col_map = {}
    for idx, h in enumerate(headers):
        # Clean header name
        h_clean = h.replace(" ", "_").replace("(", "").replace(")", "").replace(".", "")
        # Standardize aliases
        if h in ("item name", "item_name", "item", "description", "product", "name", "product_name"):
            col_map["name"] = idx
        elif h in ("qty", "quantity", "count", "units"):
            col_map["quantity"] = idx
        elif h in ("unit_price", "unitprice", "price", "rate", "each", "unit price"):
            col_map["unit_price"] = idx
        elif h in ("total_price", "totalprice", "amount", "total", "line total", "line_total", "total price"):
            col_map["total_price"] = idx
        else:
            col_map[h_clean] = idx

    # Parse metadata from other rows or from repeated columns
    # First, let's scan non-tabular rows before the header for metadata
    metadata = {}
    for idx in range(header_idx):
        row = rows[idx]
        if len(row) >= 2 and row[0].strip():
            k = row[0].strip().lower().replace(" ", "_")
            # Avoid picking up empty columns
            val = row[1].strip()
            if val:
                metadata[k] = val

    # Parse line items
    line_items_dicts = []
    # If the CSV has repeated metadata in each column (Invoize flat export)
    # we can also parse metadata from the first data row
    first_row_metadata = {}
    
    for row in data_rows:
        if not row or all(cell.strip() == "" for cell in row):
            continue
        
        # Build line item dict
        item_dict = {}
        for field, col_idx in col_map.items():
            if col_idx < len(row):
                item_dict[field] = row[col_idx].strip()
        
        # Check for repeated metadata columns in Invoize flat export
        # e.g., "Vendor", "Date", "Subtotal", "Discount", "Tax", "Tip", "Total"
        for field in ("vendor", "vendor_name", "date", "currency", "subtotal", "discount", "tax", "tip", "total", "payment_method"):
            # Check standard name or exact match in headers
            for alias in (field, field.replace("_", " "), field.replace("_", "")):
                if alias in headers:
                    idx = headers.index(alias)
                    if idx < len(row) and row[idx].strip():
                        first_row_metadata[field] = row[idx].strip()
                        break

        # Only add if it has a name and price/total
        if item_dict.get("name") or item_dict.get("item_name"):
            line_items_dicts.append(item_dict)

    # Merge metadata (non-tabular rows take priority, then repeated columns, then defaults)
    merged_data = {**first_row_metadata, **metadata}
    merged_data["line_items"] = line_items_dicts

    return map_dict_to_receipt(merged_data, default_vendor=Path(filename).stem)

def parse_json_content(text_content: str, filename: str) -> Optional[ReceiptData]:
    """Parse JSON receipt content."""
    try:
        d = json.loads(text_content)
    except json.JSONDecodeError:
        return None

    if isinstance(d, list):
        if not d:
            return None
        # If it is a list of dicts, it might be a list of line items, or multiple receipts
        if all(isinstance(x, dict) for x in d):
            # Check if it looks like a list of line items
            if any("name" in x or "item" in x for x in d):
                d = {"line_items": d}
            else:
                d = d[0]
        else:
            return None

    if not isinstance(d, dict):
        return None

    # Check if matches ReceiptData directly
    try:
        return ReceiptData.model_validate(d)
    except Exception:
        # Map fields of arbitrary JSON
        return map_dict_to_receipt(d, default_vendor=Path(filename).stem)

def parse_text_deterministically(text_content: str, filename: str, mime_type: str = "") -> Optional[ReceiptData]:
    """
    Unified entry point for deterministic offline CSV/JSON parsing.
    Returns ReceiptData if parsing succeeded, else None.
    """
    clean_mime = (mime_type or "").lower()
    fn_lower = filename.lower()

    if "json" in clean_mime or fn_lower.endswith(".json"):
        return parse_json_content(text_content, filename)
    elif "csv" in clean_mime or fn_lower.endswith(".csv"):
        return parse_csv_content(text_content, filename)
    
    # Try parsing as JSON first as a fallback for plain text files
    json_res = parse_json_content(text_content, filename)
    if json_res:
        return json_res
        
    # Try parsing as CSV as a fallback
    csv_res = parse_csv_content(text_content, filename)
    if csv_res:
        return csv_res

    return None
