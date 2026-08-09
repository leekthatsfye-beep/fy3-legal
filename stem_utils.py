"""
Unified stem normalization for the FY3 pipeline.

Every file that needs to match beat stems (uploads, sync, scheduler,
SEO, airbit, etc.) should import from here instead of defining its own.
"""

from __future__ import annotations

import re
from pathlib import Path


def safe_stem(name: str | Path) -> str:
    """Normalize a filename or beat name into a canonical stem key.

    Rules:
    1. Strip file extension (.mp3, .wav, .flp, .mp4, etc.)
    2. Lowercase
    3. Replace all non-alphanumeric chars with nothing
    4. Strip leading/trailing whitespace

    This is THE canonical stem function. Every match across YouTube logs,
    Airbit logs, store logs, metadata files, and beat files must use this.
    """
    s = str(name)
    # Strip path components
    s = s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Strip file extension
    s = re.sub(r"\.\w{2,5}$", "", s)
    # Lowercase and strip non-alphanumeric
    s = re.sub(r"[^a-z0-9]", "", s.lower().strip())
    return s


def stem_match(a: str, b: str) -> bool:
    """Check if two names refer to the same beat."""
    return safe_stem(a) == safe_stem(b)


def find_stem_in_map(needle: str, haystack: dict) -> str | None:
    """Find a stem key in a dict, trying exact then normalized match."""
    if needle in haystack:
        return needle
    norm = safe_stem(needle)
    for key in haystack:
        if safe_stem(key) == norm:
            return key
    return None


def human_stem(name: str | Path) -> str:
    """Convert a stem to a human-readable title."""
    s = str(name)
    s = s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    s = re.sub(r"\.\w{2,5}$", "", s)
    s = re.sub(r"[_-]+", " ", s)
    return s.strip().title()
