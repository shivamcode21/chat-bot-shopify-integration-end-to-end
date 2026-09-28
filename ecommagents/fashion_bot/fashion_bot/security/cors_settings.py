"""Parse CORS_ALLOWED_ORIGINS from environment (comma-separated)."""

from __future__ import annotations

import os
from typing import List


def get_cors_allowed_origins() -> List[str]:
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]
