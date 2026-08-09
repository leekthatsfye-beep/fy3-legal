#!/usr/bin/env python3
"""
sync_airbit_links.py
--------------------
Scrapes all beats from the Airbit store API, matches them to stems in
uploads_log.json, updates store_uploads_log.json with specific beat URLs,
then pushes corrected YouTube descriptions via upload.py --update.

Usage:
    python sync_airbit_links.py            # match + update YT descriptions
    python sync_airbit_links.py --dry-run  # show matches without writing
"""

import json
import re
import sys
import time
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
AIRBIT_USER_ID   = 455547
AIRBIT_BASE      = "https://api.airbit.com"
STORE_BASE_URL   = "https://leekthatsfy3.infinity.airbit.com"
PAGE_SIZE        = 50
PROJECT_DIR      = Path(__file__).parent
UPLOADS_LOG      = PROJECT_DIR / "uploads_log.json"
STORE_LOG        = PROJECT_DIR / "store_uploads_log.json"
DRY_RUN          = "--dry-run" in sys.argv

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": f"{STORE_BASE_URL}/",
    "Origin": STORE_BASE_URL,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_stem(name: str) -> str:
    """Normalise a beat name to a filesystem/log stem."""
    try:
        from stem_utils import safe_stem as _canonical
        return _canonical(name)
    except ImportError:
        s = name.lower()
        s = re.sub(r"[^\w\s]", "", s)
        s = re.sub(r"\s+", "_", s.strip())
        return s


def fetch_all_beats() -> list[dict]:
    """Paginate through all beats in the store and return a flat list."""
    beats = []
    page  = 1
    while True:
        r = requests.get(
            f"{AIRBIT_BASE}/users/{AIRBIT_USER_ID}/beats",
            params={"limit": PAGE_SIZE, "page": page, "expand": "tags"},
            headers=HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data  = r.json()
        items = data.get("items", [])
        beats.extend(items)
        print(f"  Page {page}: fetched {len(items)} beats (total so far: {len(beats)})")
        # Airbit returns fewer items than limit on the last page
        if len(items) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.3)   # be polite
    return beats


def build_alias_map(beats: list[dict]) -> dict[str, str]:
    """Return {alias -> full_url} for every beat."""
    return {
        b["alias"]: f"{STORE_BASE_URL}/beats/{b['alias']}"
        for b in beats
    }


def build_stem_map(beats: list[dict]) -> dict[str, tuple[str, str]]:
    """Return {stem -> (alias, full_url)} derived from the beat name.

    Adds extra entries for titles that follow the common Airbit pattern:
      '[Artist] Type Beat - "[Track Name]"'
    so the short track-name stem also matches.
    """
    m = {}
    for b in beats:
        alias = b["alias"]
        url   = f"{STORE_BASE_URL}/beats/{alias}"
        full_stem = safe_stem(b["name"])
        m[full_stem] = (alias, url)

        # Strip common "Artist Type Beat - " prefix to get just the track name stem
        import re as _re
        # Matches:  Anything up to and including ' - "'  or  ' - '
        short_name = _re.sub(r'^.*?type beat\s*-\s*"?', "", b["name"], flags=_re.IGNORECASE).strip('" ')
        if short_name and short_name.lower() != b["name"].lower():
            short_stem = safe_stem(short_name)
            if short_stem and short_stem not in m:
                m[short_stem] = (alias, url)

    return m


def match_stem_to_beat(stem: str, stem_map: dict, alias_map: dict) -> tuple[str, str] | None:
    """
    Try several strategies to match a YT stem to an Airbit beat.
    Returns (alias, url) or None.
    """
    # 1. Exact stem match
    if stem in stem_map:
        return stem_map[stem]

    # 2. Alias exact match
    if stem in alias_map:
        return stem, alias_map[stem]

    # 3. Stem is a prefix of an alias (e.g. "army" matches "army-1")
    for alias, url in alias_map.items():
        alias_stem = safe_stem(alias.replace("-", " "))
        if alias_stem == stem or alias_stem.startswith(stem + "_") or stem.startswith(alias_stem + "_"):
            return alias, url

    # 4. Fuzzy: check if all words of stem appear in alias
    stem_words = set(stem.split("_"))
    best = None
    best_score = 0
    for s, (alias, url) in stem_map.items():
        s_words = set(s.split("_"))
        shared  = len(stem_words & s_words)
        if shared == len(stem_words) and shared > best_score:
            best       = (alias, url)
            best_score = shared
    if best:
        return best

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Airbit → YouTube Description Sync")
    print("=" * 60)

    # Load logs
    with open(UPLOADS_LOG) as f:
        yt_log = json.load(f)
    with open(STORE_LOG) as f:
        store_log = json.load(f)

    # Fetch all beats from Airbit
    print("\n[1/4] Fetching beats from Airbit API …")
    beats = fetch_all_beats()
    print(f"  ✓  {len(beats)} beats fetched total")

    stem_map  = build_stem_map(beats)
    alias_map = build_alias_map(beats)

    # Find stems that still have the generic store URL or no entry
    print("\n[2/4] Matching stems to specific beat URLs …")
    needs_update: list[str] = []
    matched:      dict[str, tuple[str, str]] = {}
    unmatched:    list[str] = []

    for stem, yt_data in yt_log.items():
        if "publishAt" not in yt_data:
            continue   # skip unscheduled / already published without schedule
        airbit = store_log.get(stem, {}).get("airbit", {})
        listing_id = airbit.get("listing_id", "")
        url        = airbit.get("url", "")
        has_specific = bool(listing_id) and url != f"{STORE_BASE_URL}/" and url != ""

        if has_specific:
            continue   # already good

        result = match_stem_to_beat(stem, stem_map, alias_map)
        if result:
            alias, beat_url = result
            matched[stem] = (alias, beat_url)
            needs_update.append(stem)
            print(f"  ✓  {stem:45s} → {beat_url}")
        else:
            unmatched.append(stem)
            print(f"  ✗  {stem:45s} → NO MATCH FOUND")

    print(f"\n  Matched: {len(matched)}  |  Unmatched: {len(unmatched)}")

    if not matched:
        print("\nNothing to update.")
        return

    if DRY_RUN:
        print("\n[DRY RUN] No files written.")
        return

    # Update BOTH store logs atomically via StoreLog
    print("\n[3/4] Updating store logs …")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        from store_log_utils import StoreLog
        sl = StoreLog(PROJECT_DIR)
        sl.load()
        for stem, (alias, beat_url) in matched.items():
            sl.set_airbit(stem, listing_id=alias, url=beat_url, uploaded_at=now)
        sl.save_and_sync()
        print(f"  +  Both store logs updated and synced")
    except ImportError:
        for stem, (alias, beat_url) in matched.items():
            if stem not in store_log:
                store_log[stem] = {}
            store_log[stem]["airbit"] = {
                "listing_id": alias,
                "url":        beat_url,
                "uploaded_at": now,
            }
        with open(STORE_LOG, "w") as f:
            json.dump(store_log, f, indent=2)
        print(f"  +  store_uploads_log.json updated")

    # Also update the metadata JSON descriptions on disk
    print("\n[4/4] Updating metadata JSON descriptions …")
    META_DIR = PROJECT_DIR / "metadata"
    desc_updated = []
    for stem, (alias, beat_url) in matched.items():
        meta_path = META_DIR / f"{stem}.json"
        if not meta_path.exists():
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        desc = meta.get("description", "")
        if "AIRBIT_LINK_HERE" in desc or "leekthatsfy3.infinity.airbit.com/" == desc.split("\n")[1].strip() if len(desc.split("\n")) > 1 else False:
            new_desc = desc.replace("AIRBIT_LINK_HERE", beat_url)
            # Also replace bare store homepage with specific link
            new_desc = new_desc.replace(f"{STORE_BASE_URL}/\n", f"{beat_url}\n")
            new_desc = new_desc.replace(f"{STORE_BASE_URL}/", beat_url)
            if new_desc != desc:
                meta["description"] = new_desc
                with open(meta_path, "w") as f:
                    json.dump(meta, f, indent=2)
                desc_updated.append(stem)
    print(f"  ✓  {len(desc_updated)} metadata JSON files updated")

    # Push to YouTube via upload.py --update
    print("\n[5/5] Pushing updated descriptions to YouTube …")
    stems_str = ",".join(needs_update)
    import subprocess
    result = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "upload.py"), "--update", "--only", stems_str],
        cwd=str(PROJECT_DIR),
    )
    if result.returncode == 0:
        print(f"\n✅  Done! {len(needs_update)} YouTube descriptions updated with specific Airbit links.")
    else:
        print(f"\n⚠️  upload.py exited with code {result.returncode}")

    if unmatched:
        print(f"\n⚠️  {len(unmatched)} stems had no Airbit match (not on store yet?):")
        for s in unmatched:
            print(f"     {s}")


if __name__ == "__main__":
    main()
