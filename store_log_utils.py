"""
Unified store upload log manager.

Merges airbit_uploads_log.json and store_uploads_log.json into one
consistent view. Writes always go to store_uploads_log.json (the
canonical source). Reads merge both files so no data is lost.

This eliminates the dual-log sync problem where a beat appears
uploaded in one log but not the other.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from stem_utils import safe_stem

logger = logging.getLogger(__name__)

PLATFORMS = ("airbit", "beatstars")


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


class StoreLog:
    """Unified read/write interface for store upload logs."""

    def __init__(self, root: Path):
        self.store_log_path = root / "store_uploads_log.json"
        self.airbit_log_path = root / "airbit_uploads_log.json"
        self._data: dict[str, dict] | None = None

    def load(self) -> dict[str, dict]:
        """Load and merge both log files into canonical format.

        Canonical format per stem:
            {
                "airbit": {"listing_id": ..., "url": ..., "uploaded_at": ...},
                "beatstars": {"listing_id": ..., "url": ..., "uploaded_at": ...}
            }
        """
        store = _load_json(self.store_log_path)
        airbit = _load_json(self.airbit_log_path)
        merged: dict[str, dict] = {}

        # Process store_uploads_log.json
        for stem, data in store.items():
            if not isinstance(data, dict):
                continue
            entry = merged.setdefault(stem, {})
            # Check if it's already in platform-keyed format
            if any(p in data for p in PLATFORMS):
                for p in PLATFORMS:
                    if p in data:
                        entry[p] = data[p]
            else:
                # Legacy flat format — treat as airbit
                if data.get("url") or data.get("listing_id"):
                    entry.setdefault("airbit", {}).update(data)

        # Merge airbit_uploads_log.json (fills gaps)
        for stem, data in airbit.items():
            if not isinstance(data, dict):
                continue
            entry = merged.setdefault(stem, {})
            ab = entry.setdefault("airbit", {})
            # Only fill in missing fields — store_uploads_log.json wins
            if isinstance(data, dict):
                if data.get("url") and not ab.get("url"):
                    ab["url"] = data["url"]
                if data.get("listing_id") and not ab.get("listing_id"):
                    ab["listing_id"] = data["listing_id"]
                if data.get("uploaded_at") and not ab.get("uploaded_at"):
                    ab["uploaded_at"] = data["uploaded_at"]
                # Also check nested airbit key
                if "airbit" in data and isinstance(data["airbit"], dict):
                    nested = data["airbit"]
                    if nested.get("url") and not ab.get("url"):
                        ab["url"] = nested["url"]
                    if nested.get("listing_id") and not ab.get("listing_id"):
                        ab["listing_id"] = nested["listing_id"]

        self._data = merged
        return merged

    def save(self) -> None:
        """Write merged data back to store_uploads_log.json."""
        if self._data is not None:
            _save_json(self.store_log_path, self._data)

    def save_and_sync(self) -> None:
        """Write to store_uploads_log.json AND update airbit_uploads_log.json
        so both files stay consistent."""
        if self._data is None:
            return
        _save_json(self.store_log_path, self._data)

        # Also write airbit entries to airbit log for backwards compat
        airbit_data: dict[str, dict] = {}
        for stem, platforms in self._data.items():
            if "airbit" in platforms:
                airbit_data[stem] = {"airbit": platforms["airbit"]}
        _save_json(self.airbit_log_path, airbit_data)

    def get(self, stem: str) -> dict | None:
        """Get store data for a stem (tries normalized match)."""
        data = self._data if self._data else self.load()
        if stem in data:
            return data[stem]
        norm = safe_stem(stem)
        for key, val in data.items():
            if safe_stem(key) == norm:
                return val
        return None

    def set_airbit(self, stem: str, listing_id: str = "",
                   url: str = "", uploaded_at: str = "") -> None:
        """Record an Airbit upload for a stem."""
        if self._data is None:
            self.load()
        entry = self._data.setdefault(stem, {}).setdefault("airbit", {})
        if listing_id:
            entry["listing_id"] = listing_id
        if url:
            entry["url"] = url
        if uploaded_at:
            entry["uploaded_at"] = uploaded_at

    def is_on_airbit(self, stem: str) -> bool:
        """Check if a stem has an Airbit listing."""
        data = self.get(stem)
        if not data:
            return False
        ab = data.get("airbit", {})
        return bool(ab.get("url") or ab.get("listing_id"))

    def airbit_url(self, stem: str) -> str:
        """Get the Airbit URL for a stem, or empty string."""
        data = self.get(stem)
        if not data:
            return ""
        return data.get("airbit", {}).get("url", "")

    @property
    def all_stems(self) -> set[str]:
        if self._data is None:
            self.load()
        return set(self._data.keys())
