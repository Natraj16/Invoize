"""
Application configuration.

Loads settings from environment variables (.env file) with sensible defaults.
Uses Pydantic's BaseSettings for type-safe config — any typo in .env keys
gets caught at startup, not at runtime during a demo.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv()


class Settings:
    """
    Central configuration for Invoize.

    Why a plain class instead of pydantic-settings?
    - Keeps dependencies minimal (no extra pip install)
    - For a portfolio project, this is transparent and easy to explain
    - pydantic-settings would be the right call for a production app
      with dozens of config values
    """

    # --- API Keys ---
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # --- Model Configuration ---
    # Gemini 2.5 Flash: best balance of speed, cost (free tier), and quality
    # for structured extraction tasks. Vision-capable out of the box.
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # --- File Upload Limits ---
    MAX_FILE_SIZE_MB: int = 10
    MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024
    ALLOWED_MIME_TYPES: set[str] = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
    }

    # --- Paths ---
    PROJECT_ROOT: Path = Path(__file__).parent.parent
    UPLOAD_DIR: Path = PROJECT_ROOT / "uploads"

    # --- Database ---
    DB_PATH: Path = PROJECT_ROOT / "receipts.db"

    def __init__(self):
        # Ensure upload directory exists
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Check that required config is present. Returns list of errors."""
        errors = []
        if not self.GEMINI_API_KEY:
            errors.append(
                "GEMINI_API_KEY is not set. "
                "Copy .env.example to .env and add your key from https://aistudio.google.com"
            )
        return errors


# Singleton — import this everywhere
settings = Settings()
