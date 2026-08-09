"""
Beat Store Sync Manager — bidirectional sync between YouTube catalog
and beat stores (Airbit, BeatStars).

Compares uploaded YouTube beats against store listings to find:
- Beats on YouTube but NOT on store (missing listings)
- Beats on store but NOT on YouTube (not uploaded)
- Beats that need metadata sync (title/tags mismatch)
- Per-platform breakdown (Airbit vs BeatStars)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.backend.config import ROOT
from stem_utils import safe_stem as _safe_stem
from store_log_utils import StoreLog

logger = logging.getLogger(__name__)

PLATFORMS = ("airbit", "beatstars")

PLATFORM_INFO = {
    "airbit": {
        "name": "Airbit",
        "color": "#22c55e",
        "icon": "shopping-bag",
        "url_base": "https://airbit.com",
    },
    "beatstars": {
        "name": "BeatStars",
        "color": "#fbbf24",
        "icon": "star",
        "url_base": "https://beatstars.com",
    },
}


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


# ── Full sync scan ────────────────────────────────────────────────────────

def sync_scan(
    uploads_log_path: Path,
    store_uploads_log_path: Path,
    beats_dir: Path,
    metadata_dir: Path,
    platform: str | None = None,
) -> dict[str, Any]:
    """
    Scan and compare YouTube uploads vs store listings.
    If platform is specified, filters to just that platform.
    Otherwise returns data for all platforms.
    """
    yt_log = _load_json(uploads_log_path)

    # Use StoreLog to merge both airbit and store logs automatically
    sl = StoreLog(store_uploads_log_path.parent)
    store_log = sl.load()

    # All beat stems
    audio_files = list(beats_dir.glob("*.mp3")) + list(beats_dir.glob("*.wav"))
    all_stems = sorted({_safe_stem(f.name) for f in audio_files})

    yt_stems = set(yt_log.keys())

    # Per-platform store stems (StoreLog already normalizes the format)
    platform_stems: dict[str, set[str]] = {p: set() for p in PLATFORMS}
    for stem, platforms_data in store_log.items():
        if isinstance(platforms_data, dict):
            for p in PLATFORMS:
                if p in platforms_data:
                    platform_stems[p].add(stem)

    # Combined store stems (on ANY store)
    any_store_stems = set()
    for s in platform_stems.values():
        any_store_stems |= s

    # Filter platforms if requested
    active_platforms = [platform] if platform and platform in PLATFORMS else list(PLATFORMS)

    # ── Build per-beat detailed data ──────────────────────────────────
    beats_detail: list[dict[str, Any]] = []

    for stem in all_stems:
        meta_path = metadata_dir / f"{stem}.json"
        meta = _load_json(meta_path) if meta_path.exists() else {}
        yt_info = yt_log.get(stem, {})

        on_youtube = stem in yt_stems
        platform_status: dict[str, dict[str, Any]] = {}

        for p in active_platforms:
            on_platform = stem in platform_stems[p]
            store_entry = {}
            if on_platform:
                raw = store_log.get(stem, {})
                store_entry = raw.get(p, raw) if isinstance(raw, dict) else {}

            # Check metadata sync
            yt_title = yt_info.get("title", "")
            meta_title = meta.get("title", "")
            titles_match = (
                not yt_title
                or not meta_title
                or yt_title.lower().strip() == meta_title.lower().strip()
            )

            platform_status[p] = {
                "listed": on_platform,
                "url": store_entry.get("url", ""),
                "listing_id": store_entry.get("listing_id", ""),
                "uploaded_at": store_entry.get("uploaded_at", ""),
                "synced": titles_match if on_platform else None,
            }

        # Determine overall status
        on_any_store = any(ps["listed"] for ps in platform_status.values())
        all_synced = all(
            ps.get("synced", True)
            for ps in platform_status.values()
            if ps["listed"]
        )

        if on_youtube and on_any_store and all_synced:
            status = "synced"
        elif on_youtube and on_any_store and not all_synced:
            status = "needs_update"
        elif on_youtube and not on_any_store:
            status = "missing_from_store"
        elif not on_youtube and on_any_store:
            status = "missing_from_youtube"
        else:
            status = "not_uploaded"

        beats_detail.append({
            "stem": stem,
            "title": meta.get("beat_name", "") or meta.get("title", stem.replace("_", " ").title()),
            "artist": meta.get("artist", ""),
            "seo_artist": meta.get("seo_artist", ""),
            "lane": meta.get("lane", ""),
            "bpm": meta.get("bpm", 0),
            "key": meta.get("key", ""),
            "tags": meta.get("tags", [])[:5],
            "on_youtube": on_youtube,
            "youtube_url": yt_info.get("url", ""),
            "youtube_title": yt_info.get("title", ""),
            "uploaded_at_yt": yt_info.get("uploadedAt", ""),
            "platforms": platform_status,
            "status": status,
        })

    # ── Summary stats ─────────────────────────────────────────────────
    summary: dict[str, Any] = {
        "total_beats": len(all_stems),
        "on_youtube": len(yt_stems & set(all_stems)),
        "synced": sum(1 for b in beats_detail if b["status"] == "synced"),
        "missing_from_store": sum(1 for b in beats_detail if b["status"] == "missing_from_store"),
        "missing_from_youtube": sum(1 for b in beats_detail if b["status"] == "missing_from_youtube"),
        "needs_update": sum(1 for b in beats_detail if b["status"] == "needs_update"),
        "not_uploaded": sum(1 for b in beats_detail if b["status"] == "not_uploaded"),
    }

    # Per-platform stats
    platform_summary: dict[str, dict[str, int]] = {}
    for p in active_platforms:
        listed = sum(1 for b in beats_detail if b["platforms"].get(p, {}).get("listed"))
        not_listed = sum(
            1 for b in beats_detail
            if b["on_youtube"] and not b["platforms"].get(p, {}).get("listed")
        )
        platform_summary[p] = {
            "listed": listed,
            "not_listed": not_listed,
            "total_on_youtube": summary["on_youtube"],
        }

    summary["platforms"] = platform_summary

    return {
        "summary": summary,
        "beats": beats_detail,
        "platforms": {p: PLATFORM_INFO[p] for p in active_platforms},
    }


# ── Quick sync status (lightweight, no beat details) ──────────────────────

def sync_status(
    uploads_log_path: Path,
    store_uploads_log_path: Path,
    beats_dir: Path,
) -> dict[str, Any]:
    """Lightweight sync status — just counts, no beat details."""
    yt_log = _load_json(uploads_log_path)
    store_log = _load_json(store_uploads_log_path)

    audio_files = list(beats_dir.glob("*.mp3")) + list(beats_dir.glob("*.wav"))
    all_stems = {_safe_stem(f.name) for f in audio_files}
    yt_stems = set(yt_log.keys()) & all_stems

    platform_counts: dict[str, int] = {p: 0 for p in PLATFORMS}
    for stem, data in store_log.items():
        if isinstance(data, dict):
            for p in PLATFORMS:
                if p in data:
                    platform_counts[p] += 1

    total_on_store = len({
        stem for stem, data in store_log.items()
        if isinstance(data, dict) and any(p in data for p in PLATFORMS)
    })

    return {
        "total_beats": len(all_stems),
        "on_youtube": len(yt_stems),
        "on_any_store": total_on_store,
        "platforms": platform_counts,
        "missing_from_stores": len(yt_stems) - total_on_store,
    }


# ── Bulk action helpers ───────────────────────────────────────────────────

def get_missing_from_platform(
    uploads_log_path: Path,
    store_uploads_log_path: Path,
    beats_dir: Path,
    metadata_dir: Path,
    platform: str,
) -> list[dict[str, Any]]:
    """Get beats that are on YouTube but not listed on a specific platform."""
    result = sync_scan(
        uploads_log_path, store_uploads_log_path,
        beats_dir, metadata_dir, platform=platform,
    )
    missing = []
    for beat in result["beats"]:
        if beat["on_youtube"] and not beat["platforms"].get(platform, {}).get("listed"):
            missing.append(beat)
    return missing


def get_needs_update(
    uploads_log_path: Path,
    store_uploads_log_path: Path,
    beats_dir: Path,
    metadata_dir: Path,
) -> list[dict[str, Any]]:
    """Get beats that are on both but have mismatched metadata."""
    result = sync_scan(
        uploads_log_path, store_uploads_log_path,
        beats_dir, metadata_dir,
    )
    return [b for b in result["beats"] if b["status"] == "needs_update"]
