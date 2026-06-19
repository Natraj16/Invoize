"""
SQLite Storage Layer.

Stores extracted receipt data, validation results, and file references.
Uses Python's built-in sqlite3 + aiosqlite for async compatibility.

Why SQLite instead of PostgreSQL?
- Zero setup (no database server to install or configure)
- Single file — easy to back up, share, or reset
- Perfect for a portfolio demo (interviewer can clone and run immediately)
- Built into Python — no extra dependencies for sync access
- aiosqlite wraps it for async FastAPI compatibility

Why NOT an ORM (SQLAlchemy)?
- For 2 tables, raw SQL is more transparent and interview-explainable
- No migration tool needed at this scale
- The SQL itself is a talking point: "I wrote the schema by hand because
  I wanted to understand the data model, not just generate it"
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.schemas import ExtractionResponse, ReceiptData
from app.validation import ValidationResult


def _get_connection() -> sqlite3.Connection:
    """Get a SQLite connection with row factory for dict-like access."""
    conn = sqlite3.connect(str(settings.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """
    Create tables if they don't exist.

    Called at app startup. Idempotent — safe to call multiple times.

    Two tables:
    - receipts: one row per uploaded file, stores the full extraction + validation
    - line_items: denormalized from the JSON for SQL-queryable analytics
      (e.g., "total spending by item name", "average receipt total")
    """
    conn = _get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS receipts (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            extraction_method TEXT DEFAULT 'vision_llm',
            extracted_data TEXT NOT NULL,
            validation_result TEXT,
            overall_confidence TEXT,
            needs_review INTEGER DEFAULT 0,
            total REAL,
            vendor_name TEXT,
            receipt_date TEXT,
            currency TEXT DEFAULT 'USD',
            processing_time REAL
        );

        CREATE TABLE IF NOT EXISTS line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id TEXT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            quantity REAL DEFAULT 1.0,
            unit_price REAL NOT NULL,
            total_price REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_receipts_confidence
            ON receipts(overall_confidence);
        CREATE INDEX IF NOT EXISTS idx_receipts_date
            ON receipts(receipt_date);
        CREATE INDEX IF NOT EXISTS idx_line_items_receipt
            ON line_items(receipt_id);
    """)
    conn.commit()
    conn.close()


def save_receipt(
    response: ExtractionResponse,
    validation: Optional[ValidationResult] = None,
) -> str:
    """
    Save an extraction result to the database.

    Returns the receipt ID (UUID).

    Why store the full JSON AND denormalized fields?
    - Full JSON: complete record, can re-validate or re-export later
    - Denormalized fields (total, vendor_name, date): enables SQL queries
      without parsing JSON in every query
    """
    receipt_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_connection()

    # Extract key fields for denormalized columns
    data = response.data
    validation_dict = response.validation

    conn.execute(
        """
        INSERT INTO receipts (
            id, filename, uploaded_at, extraction_method,
            extracted_data, validation_result, overall_confidence,
            needs_review, total, vendor_name, receipt_date,
            currency, processing_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            response.filename,
            now,
            response.extraction_method,
            json.dumps(data.model_dump() if data else {}),
            json.dumps(validation_dict) if validation_dict else None,
            validation_dict.get("overall_confidence") if validation_dict else None,
            1 if (validation_dict and validation_dict.get("needs_manual_review")) else 0,
            data.total if data else None,
            data.vendor_name if data else None,
            data.date if data else None,
            data.currency if data else None,
            response.processing_time_seconds,
        ),
    )

    # Insert line items
    if data and data.line_items:
        for item in data.line_items:
            conn.execute(
                """
                INSERT INTO line_items (receipt_id, name, quantity, unit_price, total_price)
                VALUES (?, ?, ?, ?, ?)
                """,
                (receipt_id, item.name, item.quantity, item.unit_price, item.total_price),
            )

    conn.commit()
    conn.close()
    return receipt_id


def get_receipt(receipt_id: str) -> Optional[dict]:
    """Get a single receipt by ID."""
    conn = _get_connection()
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    conn.close()

    if not row:
        return None

    return _row_to_dict(row)


def list_receipts(
    confidence: Optional[str] = None,
    needs_review: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """
    List receipts with optional filters.

    Used by the /receipts endpoint and the export layer.
    """
    conn = _get_connection()

    query = "SELECT * FROM receipts WHERE 1=1"
    params: list = []

    if confidence:
        query += " AND overall_confidence = ?"
        params.append(confidence)

    if needs_review is not None:
        query += " AND needs_review = ?"
        params.append(1 if needs_review else 0)

    query += " ORDER BY uploaded_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [_row_to_dict(row) for row in rows]


def get_receipt_count() -> int:
    """Get total number of receipts."""
    conn = _get_connection()
    count = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    conn.close()
    return count


def get_all_line_items(receipt_id: Optional[str] = None) -> list[dict]:
    """Get line items, optionally filtered by receipt ID."""
    conn = _get_connection()

    if receipt_id:
        rows = conn.execute(
            """
            SELECT li.*, r.vendor_name, r.receipt_date, r.currency
            FROM line_items li
            JOIN receipts r ON li.receipt_id = r.id
            WHERE li.receipt_id = ?
            ORDER BY li.id
            """,
            (receipt_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT li.*, r.vendor_name, r.receipt_date, r.currency
            FROM line_items li
            JOIN receipts r ON li.receipt_id = r.id
            ORDER BY r.uploaded_at DESC, li.id
            """,
        ).fetchall()

    conn.close()
    return [dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a dict with parsed JSON fields."""
    d = dict(row)
    # Parse JSON fields back to dicts
    if d.get("extracted_data"):
        try:
            d["extracted_data"] = json.loads(d["extracted_data"])
        except json.JSONDecodeError:
            pass
    if d.get("validation_result"):
        try:
            d["validation_result"] = json.loads(d["validation_result"])
        except json.JSONDecodeError:
            pass
    return d
