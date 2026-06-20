"""
Gemini API call helper with retry logic and exponential backoff.
Handles 429 RESOURCE_EXHAUSTED errors gracefully.
"""

import time
import asyncio
from typing import Any
from google.genai import types

async def generate_content_with_retry(
    client: Any,
    model: str,
    contents: Any,
    config: types.GenerateContentConfig,
    max_retries: int = 3,
    initial_delay: float = 2.0,
) -> Any:
    """
    Executes client.models.generate_content with retry logic and exponential backoff
    for 429 rate limit errors.
    """
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            # generate_content in standard genai.Client is sync, but we wrap it in a retry loop.
            # In an async context, this is fine because we await asyncio.sleep if it fails.
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            return response
        except Exception as e:
            err_str = str(e)
            # Detect 429 rate limits or resource exhaustion errors
            is_429 = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower()
            
            if is_429 and attempt < max_retries - 1:
                print(f"[WARNING] Gemini API 429 rate limit hit. Retrying in {delay:.1f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff
            else:
                raise e
