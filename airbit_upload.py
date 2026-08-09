#!/usr/bin/env python3
"""
airbit_upload.py — Airbit Beat Upload Bot (Selenium)

Uploads beats to Airbit store using browser automation.
Airbit has no public API, so this automates the web UI.

Usage:
    python airbit_upload.py --login              # First time: log in + calibrate
    python airbit_upload.py --dry-run            # Preview all beats without uploading
    python airbit_upload.py --only "army"        # Upload 1 beat (test first!)
    python airbit_upload.py                      # Upload all beats
    python airbit_upload.py --skip-uploaded false # Re-upload logged beats
    python airbit_upload.py --discover           # Dump all page elements for debugging

First run workflow:
    1. python airbit_upload.py --login    → Chrome opens, you log in, selectors auto-calibrate
    2. python airbit_upload.py --only "army"  → Test with 1 beat
    3. python airbit_upload.py            → Upload the rest
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException,
    ElementClickInterceptedException, StaleElementReferenceException,
    ElementNotInteractableException
)

# ── Paths ──
ROOT = Path(__file__).resolve().parent
BEATS_DIR = ROOT / "beats"
META_DIR = ROOT / "metadata"
OUT_DIR = ROOT / "output"
LOG_FILE = ROOT / "airbit_uploads_log.json"
STORE_LOG_FILE = ROOT / "store_uploads_log.json"  # Backend-unified log
SELECTORS_FILE = ROOT / ".airbit_selectors.json"
CHROME_PROFILE = ROOT / ".airbit_chrome_profile"

# ── Airbit URLs ──
# Airbit migrated dashboard to studio.airbit.com in early 2026
# Upload/beats management lives at app.airbit.com
AIRBIT_HOME = "https://airbit.com"
AIRBIT_LOGIN = "https://accounts.airbit.com"
AIRBIT_DASHBOARD = "https://studio.airbit.com"
AIRBIT_UPLOAD = "https://app.airbit.com/beats/create"
AIRBIT_STORE_URL = "https://leekthatsfy3.infinity.airbit.com/"

# ── Genre mapping (our tags → Airbit genre strings) ──
GENRE_MAP = {
    "trap": "Trap",
    "drill": "Drill",
    "hip hop": "Hip Hop",
    "hip-hop": "Hip Hop",
    "rap": "Hip Hop",
    "r&b": "R&B",
    "rnb": "R&B",
    "pop": "Pop",
    "afrobeat": "Afrobeat",
    "afrobeats": "Afrobeat",
    "reggaeton": "Reggaeton",
    "dancehall": "Dancehall",
    "soul": "Soul",
    "lo-fi": "Lo-fi",
    "lofi": "Lo-fi",
    "boom bap": "Boom Bap",
    "jersey club": "Jersey Club",
    "uk drill": "UK Drill",
    "ny drill": "NY Drill",
}

# ── Mood inference from tags/title ──
MOOD_KEYWORDS = {
    "dark": "Dark",
    "aggressive": "Aggressive",
    "hard": "Hard",
    "chill": "Chill",
    "sad": "Sad",
    "happy": "Happy",
    "energetic": "Energetic",
    "bouncy": "Bouncy",
    "melodic": "Melodic",
    "emotional": "Emotional",
    "hype": "Hype",
    "wavy": "Wavy",
    "smooth": "Smooth",
    "intense": "Intense",
    "angry": "Angry",
    "upbeat": "Upbeat",
}


def p(msg: str):
    """Print with flush."""
    print(msg, flush=True)


# ═══════════════════════════════════════════════
#  Upload Log
# ═══════════════════════════════════════════════

def load_log() -> dict:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return {}


def save_log(log: dict):
    LOG_FILE.write_text(json.dumps(log, indent=2))


def record_store_log(stem: str, listing_url: str = ""):
    """Write to BOTH store_uploads_log.json and airbit_uploads_log.json atomically."""
    try:
        from store_log_utils import StoreLog
        sl = StoreLog(ROOT)
        sl.load()
        sl.set_airbit(
            stem,
            url=listing_url or f"https://leekthatsfy3.infinity.airbit.com/beats/{stem.lower().replace('_', '-')}",
            uploaded_at=datetime.now().isoformat(),
        )
        sl.save_and_sync()
    except Exception as e:
        p(f"  [!] Failed to update store log: {e}")


# ═══════════════════════════════════════════════
#  Beat Loading & Metadata
# ═══════════════════════════════════════════════

def get_beats(only: str = None) -> list:
    """Get list of beat stems to upload."""
    seen = set()
    beats = []
    for pattern in ("*.mp3", "*.wav"):
        for f in sorted(BEATS_DIR.rglob(pattern)):
            stem = f.stem
            if stem.startswith("bg_"):
                continue  # Skip Bowl Gospel remixes
            if stem not in seen:
                seen.add(stem)
                beats.append(stem)

    if only:
        allowed = set(s.strip() for s in only.split(","))
        beats = [b for b in beats if b in allowed]

    return beats


def load_metadata(stem: str) -> dict:
    meta_path = META_DIR / f"{stem}.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {"title": stem.replace("_", " ").title(), "tags": [], "bpm": 0}


def infer_genre(meta: dict) -> str:
    tags_lower = " ".join(meta.get("tags", [])).lower()
    title_lower = meta.get("title", "").lower()
    search_text = tags_lower + " " + title_lower

    for keyword, genre in GENRE_MAP.items():
        if keyword in search_text:
            return genre
    return "Trap"


def infer_moods(meta: dict, max_moods: int = 3) -> list:
    search_text = " ".join(meta.get("tags", [])).lower() + " " + meta.get("title", "").lower()
    moods = []
    for keyword, mood in MOOD_KEYWORDS.items():
        if keyword in search_text and mood not in moods:
            moods.append(mood)
            if len(moods) >= max_moods:
                break
    if not moods:
        moods = ["Dark", "Hard"]
    return moods[:max_moods]


# ═══════════════════════════════════════════════
#  Selector Calibration System
# ═══════════════════════════════════════════════

DEFAULT_SELECTORS = {
    # File input (hidden, Selenium can still interact)
    "file_input": [
        "input[type='file'][accept*='audio']",
        "input[type='file']",
    ],
    # Beat name field (new Airbit: beats[0][name])
    "title": [
        "input[name='beats[0][name]']",
        "#beats\\[0\\]\\[name\\]",
        "input[type='text'][name*='name']",
    ],
    # BPM/Tempo field (new Airbit: beats[0][bpm])
    "bpm": [
        "input[name='beats[0][bpm]']",
        "#beats\\[0\\]\\[bpm\\]",
        "input[type='text'][name*='bpm']",
    ],
    # Tags select (new Airbit: beats[0][tags][])
    "tags": [
        "select[name='beats[0][tags][]']",
        "#beats\\[0\\]\\[tags\\]",
        "input[type='search']",  # Tag search input inside select2
    ],
    # Genre select (new Airbit: beats[0][genre])
    "genre": [
        "select[name='beats[0][genre]']",
        "#beats\\[0\\]\\[genre\\]",
    ],
    # Key select (new Airbit: beats[0][key])
    "key": [
        "select[name='beats[0][key]']",
        "#beats\\[0\\]\\[key\\]",
    ],
    # Moods select (new Airbit: beats[0][moods][])
    "moods": [
        "select[name='beats[0][moods][]']",
        "#beats\\[0\\]\\[moods\\]",
    ],
    # Submit/Save/Publish button
    "submit": [
        "button[type='submit']",
        "input[type='submit']",
    ],
}


def load_selectors() -> dict:
    """Load calibrated selectors, or defaults."""
    if SELECTORS_FILE.exists():
        try:
            saved = json.loads(SELECTORS_FILE.read_text())
            p(f"[OK] Loaded calibrated selectors from {SELECTORS_FILE.name}")
            return saved
        except Exception:
            pass
    return {}


def save_selectors(selectors: dict):
    SELECTORS_FILE.write_text(json.dumps(selectors, indent=2))
    p(f"[OK] Selectors saved to {SELECTORS_FILE.name}")


def find_element_adaptive(driver, field_name: str, calibrated: dict, timeout=5):
    """Try calibrated selector first, then fallback to defaults."""
    # Try calibrated selector
    if field_name in calibrated:
        sel = calibrated[field_name]
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el, sel
        except (NoSuchElementException, StaleElementReferenceException):
            pass

    # Fallback to defaults list
    for sel in DEFAULT_SELECTORS.get(field_name, []):
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el:
                return el, sel
        except (NoSuchElementException, StaleElementReferenceException):
            continue

    return None, None


def find_button_by_text(driver, texts):
    """Find a button by matching text content.

    Prefers an EXACT text match over substring: on Airbit's create form both
    "Save" (row-level, publishes row 0) and "Save All" (bulk-publishes every
    queued draft) exist — a substring match for "Save" could hit "Save All"
    if DOM order ever changes, which would publish 30+ junk drafts.
    """
    for exact in (True, False):
        for text in texts:
            try:
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    try:
                        btn_text = btn.text.strip().lower()
                        hit = (btn_text == text.lower()) if exact \
                            else (text.lower() in btn_text)
                        if hit and btn.is_displayed():
                            return btn
                    except StaleElementReferenceException:
                        continue
            except Exception:
                continue

    # Also try input[type=submit]
    try:
        submits = driver.find_elements(By.CSS_SELECTOR, "input[type='submit']")
        for s in submits:
            if s.is_displayed():
                return s
    except Exception:
        pass

    return None


def dismiss_cookie_banner(driver):
    """Dismiss Airbit cookie consent banner if present."""
    try:
        btn = find_button_by_text(driver, ["Reject non-essential", "Reject", "Accept all"])
        if btn:
            btn.click()
            p("  [~] Dismissed cookie banner")
            time.sleep(1)
    except Exception:
        pass


# ═══════════════════════════════════════════════
#  Browser Management
# ═══════════════════════════════════════════════

def _kill_stale_automation_chrome():
    """Kill orphaned Chrome/chromedriver holding the automation profile.

    A timeout-killed run leaves chrome.exe (with our profile) and
    undetected_chromedriver.exe behind; the next launch then fails with
    'session not created: cannot connect to chrome'. Only touches processes
    whose command line references OUR profile — never the user's browser.
    """
    if sys.platform != "win32":
        return
    import subprocess as _sp
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -match 'airbit_chrome_profile' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "Get-CimInstance Win32_Process -Filter \"Name='undetected_chromedriver.exe'\" | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        _sp.run(["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=30)
        p("  [~] Cleaned stale automation Chrome/chromedriver")
    except Exception:
        pass


def launch_browser(_retry=True):
    """Launch Chrome with undetected-chromedriver and persistent profile."""
    CHROME_PROFILE.mkdir(exist_ok=True)

    # ── Clean stale lock files that prevent Chrome from starting ──
    # Chrome leaves these behind on unclean shutdown (crash, kill, etc.)
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = CHROME_PROFILE / lock_name
        if lock.exists() or lock.is_symlink():
            try:
                lock.unlink()
                p(f"  [~] Removed stale {lock_name}")
            except OSError:
                pass

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1400,900")
    # Use random debug port so user's regular Chrome doesn't conflict
    options.add_argument("--remote-debugging-port=0")

    # Auto-detect installed Chrome version to avoid driver mismatch (cross-platform)
    import subprocess as _sp
    _chrome_ver = None
    try:
        if sys.platform == "win32":
            # Windows: read version from registry (BLBeacon), fall back to chrome.exe
            import winreg
            for _hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    _k = winreg.OpenKey(_hive, r"Software\Google\Chrome\BLBeacon")
                    _v, _ = winreg.QueryValueEx(_k, "version")
                    winreg.CloseKey(_k)
                    _chrome_ver = int(str(_v).split(".")[0])
                    break
                except OSError:
                    continue
        elif sys.platform == "darwin":
            _out = _sp.check_output(
                ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
                stderr=_sp.DEVNULL, text=True,
            ).strip()
            _chrome_ver = int(_out.split()[-1].split(".")[0])
        else:
            _out = _sp.check_output(["google-chrome", "--version"],
                                    stderr=_sp.DEVNULL, text=True).strip()
            _chrome_ver = int(_out.split()[-1].split(".")[0])
        if _chrome_ver:
            p(f"  [~] Detected Chrome version: {_chrome_ver}")
        else:
            p("  [~] Could not detect Chrome version, using default")
    except Exception:
        p("  [~] Could not detect Chrome version, using default")

    try:
        driver = uc.Chrome(options=options, version_main=_chrome_ver)
    except Exception as e:
        if _retry:
            p(f"  [~] Chrome launch failed ({str(e)[:80]}) — cleaning stale processes and retrying")
            _kill_stale_automation_chrome()
            time.sleep(3)
            return launch_browser(_retry=False)
        p(f"\n[ERROR] Failed to launch Chrome: {e}")
        p("[ERROR] Close all Chrome windows and try again.")
        sys.exit(1)

    driver.implicitly_wait(3)
    return driver


def _is_login_url(url: str) -> bool:
    return any(k in url for k in ("login", "sign", "auth", "accounts.airbit"))


def ensure_logged_in(driver) -> bool:
    """Check if logged into Airbit; if not, wait up to 3 min for the user to log in via the open browser."""
    p("[~] Checking Airbit login status...")
    driver.get(AIRBIT_DASHBOARD)
    time.sleep(8)  # Nuxt.js SPA + potential redirect
    dismiss_cookie_banner(driver)

    current = driver.current_url
    if _is_login_url(current):
        p("\n" + "=" * 60)
        p("  AIRBIT LOGIN REQUIRED")
        p("=" * 60)
        p(f"\n  Login URL: {current}")
        p("  Chrome is open — log into Airbit in that window.")
        p("  Waiting up to 3 minutes for you to finish...")

        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(3)
            try:
                current = driver.current_url
            except Exception:
                break
            if not _is_login_url(current):
                break
        else:
            p("[ERROR] Timed out waiting for Airbit login (3 min).")
            return False

        # Extra settle time for SPA to load dashboard
        time.sleep(5)
        current = driver.current_url
        if _is_login_url(current):
            p("[ERROR] Still not logged in after waiting.")
            return False

    p("[OK] Logged into Airbit")
    return True


# ═══════════════════════════════════════════════
#  Discover Mode — Dump Page Elements
# ═══════════════════════════════════════════════

def discover_upload_page(driver):
    """Deep inspection of the upload page to find all interactable elements."""
    p("\n" + "=" * 60)
    p("  AIRBIT UPLOAD PAGE DISCOVERY")
    p("=" * 60)

    driver.get(AIRBIT_UPLOAD)
    p(f"\n[~] Loading upload page...")
    time.sleep(10)  # Nuxt.js SPA needs extra load time

    p(f"\n  Page title: {driver.title}")
    p(f"  URL: {driver.current_url}\n")

    # ── Inputs ──
    inputs = driver.find_elements(By.TAG_NAME, "input")
    p(f"--- {len(inputs)} <input> elements ---")
    for i, el in enumerate(inputs):
        try:
            attrs = {
                "type": el.get_attribute("type"),
                "name": el.get_attribute("name"),
                "id": el.get_attribute("id"),
                "placeholder": el.get_attribute("placeholder"),
                "class": (el.get_attribute("class") or "")[:60],
                "visible": el.is_displayed(),
                "enabled": el.is_enabled(),
            }
            p(f"  [{i:2d}] type={attrs['type']:<10} name={attrs['name']:<15} "
              f"id={attrs['id']:<15} ph={attrs['placeholder']}")
            p(f"       class={attrs['class']}  visible={attrs['visible']}  enabled={attrs['enabled']}")
        except StaleElementReferenceException:
            p(f"  [{i:2d}] <stale element>")

    # ── Buttons ──
    buttons = driver.find_elements(By.TAG_NAME, "button")
    p(f"\n--- {len(buttons)} <button> elements ---")
    for i, el in enumerate(buttons):
        try:
            txt = el.text[:40].replace("\n", " ") if el.text else ""
            cls = (el.get_attribute("class") or "")[:60]
            p(f"  [{i:2d}] text='{txt}' class={cls}  visible={el.is_displayed()}")
        except StaleElementReferenceException:
            p(f"  [{i:2d}] <stale>")

    # ── Selects ──
    selects = driver.find_elements(By.TAG_NAME, "select")
    p(f"\n--- {len(selects)} <select> elements ---")
    for i, el in enumerate(selects):
        try:
            p(f"  [{i:2d}] name={el.get_attribute('name')} id={el.get_attribute('id')}")
            # Show options
            opts = el.find_elements(By.TAG_NAME, "option")
            for o in opts[:10]:
                p(f"       option: value='{o.get_attribute('value')}' text='{o.text}'")
        except StaleElementReferenceException:
            pass

    # ── Textareas ──
    textareas = driver.find_elements(By.TAG_NAME, "textarea")
    p(f"\n--- {len(textareas)} <textarea> elements ---")
    for i, el in enumerate(textareas):
        try:
            p(f"  [{i:2d}] name={el.get_attribute('name')} ph={el.get_attribute('placeholder')}")
        except StaleElementReferenceException:
            pass

    # ── File inputs ──
    file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    p(f"\n--- {len(file_inputs)} file input(s) ---")
    for i, el in enumerate(file_inputs):
        try:
            p(f"  [{i:2d}] name={el.get_attribute('name')} accept={el.get_attribute('accept')}")
        except StaleElementReferenceException:
            pass

    # ── Clickable divs (React components often use divs instead of inputs) ──
    clickable = driver.find_elements(By.CSS_SELECTOR, "[role='button'], [role='combobox'], [role='listbox']")
    p(f"\n--- {len(clickable)} role-based clickable elements ---")
    for i, el in enumerate(clickable[:20]):
        try:
            role = el.get_attribute("role")
            txt = el.text[:40].replace("\n", " ") if el.text else ""
            p(f"  [{i:2d}] role={role} text='{txt}'")
        except StaleElementReferenceException:
            pass

    # ── Drag & drop zones ──
    dropzones = driver.find_elements(By.CSS_SELECTOR, "[class*='drop'], [class*='upload'], [class*='drag']")
    p(f"\n--- {len(dropzones)} drop/upload zone elements ---")
    for i, el in enumerate(dropzones[:10]):
        try:
            cls = (el.get_attribute("class") or "")[:80]
            p(f"  [{i:2d}] class={cls}")
        except StaleElementReferenceException:
            pass

    p(f"\n{'=' * 60}")
    p("  Discovery complete. Use these to update selectors.")
    p(f"{'=' * 60}")


# ═══════════════════════════════════════════════
#  Calibrate Mode — Auto-find and save selectors
# ═══════════════════════════════════════════════

def calibrate_selectors(driver) -> dict:
    """Navigate to upload page and auto-detect which selectors work.
    Saves results so future runs are faster."""
    p("\n[~] Calibrating selectors on Airbit upload page...")
    driver.get(AIRBIT_UPLOAD)
    time.sleep(10)
    dismiss_cookie_banner(driver)

    # Click "New" choice to reveal upload form
    try:
        driver.execute_script(
            'var el = document.querySelector(\'[data-choice="new"]\');'
            'if (el) el.click();'
        )
        p("  [~] Clicked 'New' upload choice")
        time.sleep(3)
    except Exception:
        pass

    calibrated = {}

    # ── Check for key form elements ──
    field_checks = {
        "file_input": "input[type='file']",
        "title": "input[name='beats[0][name]']",
        "bpm": "input[name='beats[0][bpm]']",
        "genre": "select[name='beats[0][genre]']",
        "moods": "select[name='beats[0][moods][]']",
        "tags": "select[name='beats[0][tags][]']",
        "key": "select[name='beats[0][key]']",
    }

    for field_name, selector in field_checks.items():
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            calibrated[field_name] = selector
            p(f"  [+] {field_name}: {selector}")
        except NoSuchElementException:
            p(f"  [-] {field_name}: not found")

    # ── Find submit button ──
    btn = find_button_by_text(driver, ["Save", "Publish"])
    if btn:
        btn_text = btn.text.strip()
        p(f"  [+] Submit button: '{btn_text}'")
        calibrated["submit_text"] = btn_text

    # ── Summary ──
    found = len(calibrated)
    total = len(field_checks) + 1  # +1 for submit
    p(f"\n  Calibration: {found}/{total} fields found")

    if found < 3:
        p("\n  [WARN] Very few fields found. The upload page may not have loaded fully.")
        p("  Try --discover mode for a full page dump.")

    save_selectors(calibrated)
    return calibrated


# ═══════════════════════════════════════════════
#  Upload a Single Beat
# ═══════════════════════════════════════════════

def safe_type(driver, element, text, clear=True):
    """Safely type into an element, handling React controlled inputs."""
    try:
        if clear:
            # For React inputs: select all + delete first.
            # Use the platform select-all modifier — Cmd on macOS, Ctrl on
            # Windows/Linux. (The Mac-only Keys.COMMAND was a no-op on Windows,
            # so the field never cleared and new text appended to old.)
            _selall = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
            element.click()
            time.sleep(0.2)
            ActionChains(driver).key_down(_selall).send_keys('a').key_up(_selall).perform()
            time.sleep(0.1)
            ActionChains(driver).send_keys(Keys.DELETE).perform()
            time.sleep(0.1)
            # Belt-and-suspenders for stubborn React controlled inputs
            try:
                if element.get_attribute("value"):
                    element.send_keys(Keys.CONTROL, "a")
                    element.send_keys(Keys.DELETE)
            except Exception:
                pass
        element.send_keys(text)
        return True
    except (ElementNotInteractableException, StaleElementReferenceException):
        return False


def _capture_airbit_short_link(driver, title: str, stem: str) -> str:
    """After uploading, navigate to beats management and capture the air.bi short link.

    Returns the air.bi short URL string (e.g. "https://air.bi/JpKme"),
    or empty string if not found.
    """
    import re as _re
    short_link = ""
    MAX_LINK_RETRIES = 5
    LINK_RETRY_WAIT = 30
    try:
        for _link_attempt in range(MAX_LINK_RETRIES):
            if _link_attempt > 0:
                p(f"  [~] Retry {_link_attempt}/{MAX_LINK_RETRIES-1} — waiting {LINK_RETRY_WAIT}s for listing to appear...")
                time.sleep(LINK_RETRY_WAIT)
            p("  [~] Looking for air.bi short link...")
            driver.get("https://app.airbit.com/beats")
            time.sleep(6)
            dismiss_cookie_banner(driver)

        # Scroll to load all beats (lazy-load)
        for _ in range(5):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)

        # Find the beat row by matching title
        # Airbit beats list: <h4><a href="/beats/{id}/edit">Title</a></h4>
        title_links = driver.find_elements(By.CSS_SELECTOR, "h4 a[href*='/beats/'][href*='/edit']")

        # Normalize title for fuzzy match
        def _norm(s):
            s = s.lower().strip()
            # Strip artist prefix "Artist Type Beat - " and quotes
            s = _re.sub(r'^.+?type\s*beat\s*[-–]\s*', '', s)
            s = s.replace('"', '').replace('\u201c', '').replace('\u201d', '').replace("'", "").replace("!", "").strip()
            return s

        # Also try normalizing by stem (spaces→_)
        stem_norm = stem.lower().strip()
        title_norm = _norm(title)

        beat_edit_url = ""
        for link in title_links:
            try:
                link_text = link.text.strip()
                href = link.get_attribute("href") or ""
                if not href or not link_text:
                    continue
                link_norm = _norm(link_text)
                # Match if normalized text aligns with our beat
                if (link_norm and title_norm and link_norm == title_norm) or \
                   (stem_norm and stem_norm.replace("_", "") in link_norm.replace(" ", "").replace("-", "")):
                    beat_edit_url = href
                    p(f"  [+] Found beat: '{link_text}'")
                    break
            except StaleElementReferenceException:
                continue

        # NOTE: deliberately NO most-recent-beat fallback here. If our beat
        # isn't in the list (e.g. its upload failed), the most recent listing
        # is a DIFFERENT beat — capturing its link would poison our metadata
        # and YouTube description with the wrong store URL.

        if not beat_edit_url:
            if _link_attempt < MAX_LINK_RETRIES - 1:
                p("  [~] Beat not in list yet, will retry...")
                continue
            p("  [WARN] Could not find beat in management list after retries")
            return ""

        # Navigate to the beat's edit page
        driver.get(beat_edit_url)
        time.sleep(5)

        # Look for air.bi short link on the edit/share section
        # Airbit shows a "Short Link" or "Share" input with the air.bi URL
        # Common selectors: input containing "air.bi", element with class *share*, *short*
        short_link_candidates = []

        # 1. Try to find any input/text element containing "air.bi"
        all_inputs = driver.find_elements(By.TAG_NAME, "input")
        for inp in all_inputs:
            try:
                val = inp.get_attribute("value") or ""
                if "air.bi" in val:
                    short_link_candidates.append(val.strip())
            except StaleElementReferenceException:
                continue

        # 2. Try visible text elements (spans, divs, anchors) containing "air.bi"
        if not short_link_candidates:
            for tag in ("a", "span", "div", "p"):
                els = driver.find_elements(By.TAG_NAME, tag)
                for el in els:
                    try:
                        txt = el.text.strip()
                        href = el.get_attribute("href") or ""
                        if "air.bi" in txt:
                            short_link_candidates.append(txt)
                        elif "air.bi" in href:
                            short_link_candidates.append(href)
                    except StaleElementReferenceException:
                        continue
                if short_link_candidates:
                    break

        # 3. Try page source as last resort
        if not short_link_candidates:
            try:
                source = driver.page_source
                found = _re.findall(r'https?://air\.bi/[A-Za-z0-9_\-]+', source)
                short_link_candidates.extend(found)
            except Exception:
                pass

        if short_link_candidates:
            # Pick first valid air.bi URL
            for candidate in short_link_candidates:
                # Extract URL pattern
                match = _re.search(r'https?://air\.bi/[A-Za-z0-9_\-]+', candidate)
                if match:
                    short_link = match.group(0)
                    break

        if short_link:
            p(f"  [+] air.bi short link: {short_link}")
            break  # got it, exit retry loop
        else:
            if _link_attempt < MAX_LINK_RETRIES - 1:
                p("  [~] Link not on edit page yet, will retry...")
                continue
            p("  [INFO] air.bi short link not found after retries — will use store URL")

    except Exception as e:
        p(f"  [WARN] Short link capture failed: {e}")

    return short_link


def _save_airbit_url_to_metadata(stem: str, airbit_url: str):
    """Save airbit_url field to metadata/{stem}.json."""
    meta_path = META_DIR / f"{stem}.json"
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        if meta.get("airbit_url") == airbit_url:
            return  # Already saved
        meta["airbit_url"] = airbit_url
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        p(f"  [+] Saved airbit_url to metadata/{stem}.json")
    except Exception as e:
        p(f"  [WARN] Could not save airbit_url to metadata: {e}")


def _norm_name(s: str) -> str:
    """Normalize a beat name/stem/filename for fuzzy comparison."""
    try:
        from stem_utils import safe_stem
        return safe_stem(s)
    except ImportError:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resolve_beat_path(stem: str):
    matches = list(BEATS_DIR.rglob(f"{stem}.mp3")) or list(BEATS_DIR.rglob(f"{stem}.wav"))
    return matches[0] if matches else BEATS_DIR / f"{stem}.mp3"


def _beat_row_name_inputs(driver):
    return driver.find_elements(
        By.CSS_SELECTOR, "input[name^='beats['][name$='][name]']")


def _find_edit_url(driver, stem: str, title: str) -> str:
    """Locate a beat's edit URL on the app.airbit.com/beats management list."""
    driver.get("https://app.airbit.com/beats")
    time.sleep(6)
    dismiss_cookie_banner(driver)
    for _ in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
    want = _norm_name(title) or _norm_name(stem)
    for link in driver.find_elements(By.CSS_SELECTOR, "h4 a[href*='/beats/'][href*='/edit']"):
        try:
            got = _norm_name(link.text)
            if got and (want in got or got in want):
                return link.get_attribute("href") or ""
        except StaleElementReferenceException:
            continue
    return ""


def _audio_duration_seconds(path) -> float:
    """Duration via ffprobe; 0.0 on failure."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def verify_listing_audio(driver, stem: str, title: str) -> tuple[bool, str]:
    """Verify the audio Airbit actually has for this beat matches the local file.

    Downloads the untagged MP3 from the listing's edit page (Files tab) and
    compares md5 against the local beat; falls back to a duration check when
    Airbit transcoded the file (e.g. WAV uploads). Returns (ok, message).
    """
    local = _resolve_beat_path(stem)
    if not local.exists():
        return False, f"local beat missing: {local}"

    # A just-published listing can take ~30-60s to appear in the management
    # list — retry the lookup before declaring failure.
    edit_url = ""
    for attempt in range(3):
        edit_url = _find_edit_url(driver, stem, title)
        if edit_url:
            break
        if attempt < 2:
            p(f"  [~] Listing not in management list yet (try {attempt + 1}/3) — waiting 25s")
            time.sleep(25)
    if not edit_url:
        return False, "beat not found in Airbit management list"

    driver.get(edit_url)
    time.sleep(12)
    # Open the Files tab (React tab nav — plain anchors with text 'Files')
    driver.execute_script("""
        const els = [...document.querySelectorAll('a, button, li')]
            .filter(e => (e.textContent||'').trim() === 'Files');
        if (els.length) els[els.length-1].click();
    """)
    time.sleep(4)
    dl_href = driver.execute_script("""
        const a = [...document.querySelectorAll('a')].find(e =>
            e.textContent.trim() === 'MP3' && (e.href||'').includes('/beats/download/'));
        return a ? a.href : null;
    """)
    if not dl_href:
        return False, f"no MP3 download link on edit page ({edit_url})"

    tmp = Path(tempfile.gettempdir()) / f"airbit_verify_{stem}.mp3"
    try:
        with urllib.request.urlopen(dl_href, timeout=120) as resp:
            tmp.write_bytes(resp.read())
    except Exception as e:
        return False, f"could not download Airbit MP3: {e}"

    remote_md5 = hashlib.md5(tmp.read_bytes()).hexdigest()
    local_md5 = hashlib.md5(local.read_bytes()).hexdigest()
    if remote_md5 == local_md5:
        return True, f"AUDIO MATCH (md5 {local_md5[:8]})"

    # Airbit may transcode (WAV→MP3): fall back to duration comparison
    remote_dur = _audio_duration_seconds(tmp)
    local_dur = _audio_duration_seconds(local)
    if remote_dur and local_dur and abs(remote_dur - local_dur) <= 1.5:
        return True, (f"AUDIO MATCH by duration ({remote_dur:.1f}s vs {local_dur:.1f}s; "
                      f"md5 differs — Airbit transcode)")
    return False, (f"AUDIO MISMATCH — Airbit has {remote_dur:.1f}s "
                   f"(md5 {remote_md5[:8]}), local is {local_dur:.1f}s "
                   f"(md5 {local_md5[:8]}). WRONG BEAT ON LISTING.")


def upload_beat(driver, stem: str, meta: dict, calibrated: dict) -> bool | str:
    """Upload a single beat to Airbit.

    Returns True/str on success (str = listing URL if captured), False on failure.
    """
    matches = list(BEATS_DIR.rglob(f"{stem}.mp3")) or list(BEATS_DIR.rglob(f"{stem}.wav"))
    beat_path = matches[0] if matches else BEATS_DIR / f"{stem}.mp3"
    if not beat_path.exists():
        p(f"  [FAIL] Beat file missing: {beat_path}")
        return False

    # Use beat_name for Airbit (not the full YouTube SEO title)
    title = meta.get("beat_name", stem.replace("_", " ").title())
    bpm = meta.get("bpm", 0)
    key_sig = meta.get("key", "")
    genre = infer_genre(meta)
    moods = infer_moods(meta)
    tags = meta.get("tags", [])[:10]

    p(f"  Title: {title}")
    p(f"  BPM: {bpm} | Key: {key_sig} | Genre: {genre}")
    p(f"  Moods: {', '.join(moods)} | Tags: {len(tags)}")

    try:
        # ── Navigate to upload page ──
        driver.get(AIRBIT_UPLOAD)
        time.sleep(10)
        dismiss_cookie_banner(driver)

        # ── Click "New" upload choice to reveal dropzone ──
        try:
            driver.execute_script(
                'var el = document.querySelector(\'[data-choice="new"]\');'
                'if (el) el.click();'
            )
            p("  [+] Selected 'New' upload mode")
            time.sleep(3)
        except Exception:
            p("  [WARN] Could not click New choice, trying anyway...")

        # ── Step 0.5: RECORD PRE-UPLOAD QUEUE STATE ──
        # Airbit's create form keeps EVERY previously-uploaded file as a
        # persistent draft row (35+ observed). New uploads land at beats[0].
        # The 2026-08-05 h00dr1ch drop shipped the wrong beat because the
        # form was filled while beats[0] still held the PREVIOUS drop's
        # audio. So: remember what row 0 is now, and only proceed once row 0
        # provably becomes OUR file (checked after upload below).
        pre_rows = _beat_row_name_inputs(driver)
        pre_top = ""
        if pre_rows:
            try:
                pre_top = pre_rows[0].get_attribute("value") or ""
            except StaleElementReferenceException:
                pass
            p(f"  [INFO] Upload queue holds {len(pre_rows)} old draft row(s); "
              f"current top: '{pre_top}'")

        # ── Step 1: Upload MP3 file ──
        file_el = None
        # Find the audio file input (not artwork)
        all_file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        for fi in all_file_inputs:
            accept = (fi.get_attribute("accept") or "").lower()
            name = (fi.get_attribute("name") or "").lower()
            if "audio" in accept or "mp3" in accept or "wav" in accept:
                file_el = fi
                break
            if "artwork" not in name and fi.get_attribute("type") == "file":
                file_el = fi  # fallback

        if file_el:
            driver.execute_script(
                "arguments[0].style.display = 'block'; "
                "arguments[0].style.visibility = 'visible'; "
                "arguments[0].style.opacity = '1';",
                file_el
            )
            time.sleep(0.3)
            file_el.send_keys(str(beat_path.resolve()))
            p(f"  [+] File uploaded")
        else:
            p("  [FAIL] Could not find audio file input.")
            return False

        # Wait for file upload to process (Airbit converts to 320kbps MP3)
        p("  [~] Waiting for Airbit to process file...")
        time.sleep(12)

        # ── Step 1.2: FORM-INTEGRITY GUARD ──
        # Metadata is filled into beats[0][...] — so row 0 MUST be our freshly
        # uploaded file before we touch anything. Airbit prefills the row name
        # from the uploaded filename; wait until the top row's prefill matches
        # our stem. Airbit's server-side processing has taken 3+ minutes on
        # recent drops (d33p charg3, kant br3ath3), so the window is generous.
        # If it never lands, Save would publish whatever stale draft sits at
        # row 0 — abort.
        def _row0_matches():
            try:
                top = driver.find_element(
                    By.CSS_SELECTOR, "input[name='beats[0][name]']")
                pre = top.get_attribute("value") or ""
            except (NoSuchElementException, StaleElementReferenceException):
                pre = ""
            ok = bool(pre) and (_norm_name(stem) in _norm_name(pre)
                                or _norm_name(pre) in _norm_name(stem))
            return ok, pre

        matched, prefill = False, ""
        # Airbit's server-side processing regularly takes several minutes and
        # the row only appears after a page load — so wait, then reload the
        # create form (the queued upload is server-side; reloading does NOT
        # re-upload) and wait again, up to 3 rounds (~12 min total).
        for attempt in range(3):
            deadline = time.time() + (420 if attempt == 0 else 150)
            while time.time() < deadline:
                matched, prefill = _row0_matches()
                if matched:
                    break
                time.sleep(5)
            if matched:
                break
            p(f"  [~] Row 0 is '{prefill}' after wait #{attempt + 1} — reloading create form")
            driver.get(AIRBIT_UPLOAD)
            time.sleep(10)
            dismiss_cookie_banner(driver)
            try:
                driver.execute_script(
                    'var el = document.querySelector(\'[data-choice="new"]\');'
                    'if (el) el.click();'
                )
                time.sleep(3)
            except Exception:
                pass
            matched, prefill = _row0_matches()
            if matched:
                break
        if not matched:
            p(f"  [FAIL] Top row is '{prefill}', not our file '{stem}' — the upload "
              "did not land at beats[0] after extended waits. Aborting before Save "
              "so the wrong audio can't be published.")
            return False
        p(f"  [+] Form integrity OK — beats[0] is our file (prefill '{prefill}')")

        # ── Step 1.5: Upload cover art (thumbnail) ──
        thumb_path = OUT_DIR / f"{stem}_thumb.jpg"
        if thumb_path.exists():
            try:
                # Re-scan file inputs now that the form is visible
                art_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
                art_el = None
                for fi in art_inputs:
                    name = (fi.get_attribute("name") or "").lower()
                    accept = (fi.get_attribute("accept") or "").lower()
                    if "artwork" in name or ("image" in accept and "audio" not in accept):
                        art_el = fi
                        break
                if art_el:
                    driver.execute_script(
                        "arguments[0].style.display='block';"
                        "arguments[0].style.visibility='visible';"
                        "arguments[0].style.opacity='1';"
                        "arguments[0].style.height='auto';"
                        "arguments[0].style.width='auto';",
                        art_el,
                    )
                    time.sleep(0.3)
                    art_el.send_keys(str(thumb_path.resolve()))
                    p(f"  [+] Cover art uploaded: {thumb_path.name}")
                    time.sleep(3)
                else:
                    p("  [INFO] No artwork file input found — skipping cover art")
            except Exception as e:
                p(f"  [WARN] Cover art upload failed: {e}")
        else:
            p(f"  [INFO] No thumbnail found at {thumb_path.name} — skipping cover art")

        # ── Step 2: Fill beat name ──
        try:
            name_el = driver.find_element(By.CSS_SELECTOR, "input[name='beats[0][name]']")
            if safe_type(driver, name_el, title):
                p(f"  [+] Name: '{title}'")
        except (NoSuchElementException, StaleElementReferenceException):
            p("  [WARN] Could not find name field")

        time.sleep(0.3)

        # ── Step 3: Fill BPM/Tempo ──
        if bpm:
            try:
                bpm_el = driver.find_element(By.CSS_SELECTOR, "input[name='beats[0][bpm]']")
                if safe_type(driver, bpm_el, str(int(bpm))):
                    p(f"  [+] BPM: {int(bpm)}")
            except (NoSuchElementException, StaleElementReferenceException):
                p("  [WARN] Could not find BPM field")

        time.sleep(0.3)

        # ── Step 4: Set Genre (native <select>) ──
        try:
            from selenium.webdriver.support.ui import Select
            genre_el = driver.find_element(By.CSS_SELECTOR, "select[name='beats[0][genre]']")
            sel_obj = Select(genre_el)
            # Try exact match first, then partial
            matched = False
            for option in sel_obj.options:
                if genre.lower() == option.text.strip().lower():
                    sel_obj.select_by_visible_text(option.text)
                    p(f"  [+] Genre: {option.text}")
                    matched = True
                    break
            if not matched:
                for option in sel_obj.options:
                    if genre.lower() in option.text.strip().lower():
                        sel_obj.select_by_visible_text(option.text)
                        p(f"  [+] Genre: {option.text} (partial)")
                        matched = True
                        break
            if not matched:
                # Default to Hip Hop
                sel_obj.select_by_visible_text("Hip Hop")
                p("  [+] Genre: Hip Hop (fallback)")
        except (NoSuchElementException, StaleElementReferenceException) as e:
            p(f"  [WARN] Genre: {e}")

        time.sleep(0.3)

        # ── Step 5: Set Moods (native <select> multi) ──
        if moods:
            try:
                mood_el = driver.find_element(By.CSS_SELECTOR, "select[name='beats[0][moods][]']")
                sel_obj = Select(mood_el)
                filled_moods = 0
                for mood in moods[:3]:  # Max 3
                    for option in sel_obj.options:
                        if mood.lower() == option.text.strip().lower():
                            sel_obj.select_by_visible_text(option.text)
                            filled_moods += 1
                            break
                if filled_moods:
                    p(f"  [+] Moods: {filled_moods} set")
            except (NoSuchElementException, StaleElementReferenceException):
                p("  [WARN] Could not find moods select")

        time.sleep(0.3)

        # ── Step 6: Set Key ──
        if key_sig:
            try:
                key_el = driver.find_element(By.CSS_SELECTOR, "select[name='beats[0][key]']")
                sel_obj = Select(key_el)
                # Map our key format to Airbit's: "C Minor" → "C min"
                key_mapped = key_sig.replace("Major", "maj").replace("Minor", "min").replace("major", "maj").replace("minor", "min")
                matched = False
                for option in sel_obj.options:
                    opt_text = option.text.strip()
                    if key_mapped.lower() in opt_text.lower():
                        sel_obj.select_by_visible_text(opt_text)
                        p(f"  [+] Key: {opt_text}")
                        matched = True
                        break
                if not matched:
                    p(f"  [INFO] Key '{key_sig}' not matched in options")
            except (NoSuchElementException, StaleElementReferenceException):
                p("  [WARN] Could not find key select")

        time.sleep(0.3)

        # ── Step 7: Add Tags (select2/search-based) ──
        if tags:
            try:
                # Airbit uses a select2-style tag input — find the search box inside it
                tag_search_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='search']")
                tag_input = None
                for si in tag_search_inputs:
                    try:
                        if si.is_displayed():
                            # Check if it's near the tags label
                            tag_input = si
                    except StaleElementReferenceException:
                        continue

                if not tag_input:
                    # Try clicking the tags select to reveal search
                    tags_select = driver.find_element(By.CSS_SELECTOR, "select[name='beats[0][tags][]']")
                    # Click the select2 container near it
                    driver.execute_script("arguments[0].scrollIntoView(true);", tags_select)
                    time.sleep(0.3)

                # Find visible search inputs again
                tag_search_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='search']")
                # Use the last visible search input (usually the tags one)
                for si in reversed(tag_search_inputs):
                    try:
                        if si.is_displayed():
                            tag_input = si
                            break
                    except StaleElementReferenceException:
                        continue

                if tag_input:
                    filled = 0
                    for tag in tags[:10]:
                        tag_input.click()
                        time.sleep(0.2)
                        tag_input.send_keys(tag)
                        time.sleep(0.3)
                        tag_input.send_keys(Keys.ENTER)
                        time.sleep(0.3)
                        filled += 1
                    p(f"  [+] {filled} tags added")
                else:
                    p("  [WARN] Could not find tag search input")
            except (NoSuchElementException, StaleElementReferenceException) as e:
                p(f"  [WARN] Tags: {e}")

        time.sleep(1)

        # ── Step 8: Click Save ──
        # "Save All" is deliberately NOT a candidate: it bulk-publishes every
        # stale draft in the queue (30+ junk listings), not just our row.
        btn = find_button_by_text(driver, [
            "Save", "Publish", "Submit",
        ])

        if btn:
            try:
                # Scroll to button first
                driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.5)
                btn.click()
                btn_text = btn.text.strip()
                p(f"  [+] Clicked '{btn_text}'")
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", btn)
                p(f"  [+] Clicked save (JS fallback)")
        else:
            p("  [WARN] No save button found.")

        # Wait for save
        time.sleep(5)

        # Check for success indicators
        try:
            success_indicators = driver.find_elements(By.CSS_SELECTOR,
                "[class*='success'], [class*='toast'], [class*='alert-success']")
            for ind in success_indicators:
                if ind.is_displayed():
                    txt = ind.text[:80] if ind.text else ""
                    if txt:
                        p(f"  [+] Status: {txt}")
                    break
        except Exception:
            pass

        # Try to capture listing URL from page redirect
        listing_url = ""
        try:
            current = driver.current_url
            # If Airbit redirected to a beat page, use that URL
            if "beats" in current and current != AIRBIT_UPLOAD:
                listing_url = current
                p(f"  [+] Listing URL: {listing_url}")
        except Exception:
            pass

        # Default to beat-specific URL derived from stem, not root store URL
        if not listing_url:
            import re as _re
            _slug = _re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
            listing_url = f"https://leekthatsfy3.infinity.airbit.com/beats/{_slug}"

        # ── Capture air.bi short link ──
        short_link = _capture_airbit_short_link(driver, title, stem)
        if short_link:
            _save_airbit_url_to_metadata(stem, short_link)
            listing_url = short_link  # Use short link as canonical URL

        p(f"  [OK] Upload flow completed for: {title}")
        return listing_url or True

    except Exception as e:
        p(f"  [FAIL] Upload error: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════
#  Fix Airbit Titles — strip "Type Beat" prefix
# ═══════════════════════════════════════════════

def fix_airbit_titles(driver=None) -> dict:
    """Navigate to Airbit beats management and rename beats that have 'Type Beat' in title.

    Changes titles like 'BiggKutt8 Type Beat - "Army"' to just 'Army'.

    Strategy:
        1. Load the beats list page at app.airbit.com/beats
        2. Collect edit URLs + old titles for beats containing "Type Beat"
           (store URLs as strings — NOT element references, which go stale)
        3. Navigate to each edit page individually
        4. Clear input[name='name'], type new name, click Save
        5. Navigate back to list and continue

    Returns dict: { slug: {"old_title": "...", "new_title": "..."}, ... }
    """
    import re as _re

    close_driver = False
    if driver is None:
        driver = launch_browser()
        close_driver = True

    renamed = {}
    try:
        # ── Step 1: Load beats list page ──
        p("[~] Navigating to Airbit beats management...")
        driver.get("https://app.airbit.com/beats")
        time.sleep(8)
        dismiss_cookie_banner(driver)

        # Scroll to load all beats (table may lazy-load rows)
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(10):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # ── Step 2: Collect edit URLs for beats with "Type Beat" ──
        # Airbit beats list has <h4><a href="/beats/{id}/edit">Title</a></h4>
        title_links = driver.find_elements(By.CSS_SELECTOR, "h4 a[href*='/beats/'][href*='/edit']")
        beats_to_fix = []
        for link in title_links:
            try:
                text = link.text.strip()
                href = link.get_attribute("href") or ""
                if not text or not href:
                    continue
                if "type beat" not in text.lower():
                    continue
                # Extract clean beat name: "Artist Type Beat - \"Name\"" → "Name"
                match = _re.search(r'[–\-]\s*["\u201c]?(.+?)["\u201d]?\s*$', text)
                if match:
                    clean_name = match.group(1).strip().strip('"').strip('\u201c\u201d')
                    if clean_name:
                        beats_to_fix.append({
                            "edit_url": href,
                            "old_title": text,
                            "new_title": clean_name,
                        })
            except StaleElementReferenceException:
                continue

        if not beats_to_fix:
            p("  [OK] No beats with 'Type Beat' in title found — all clean!")
            return renamed

        p(f"  [!] Found {len(beats_to_fix)} beats with 'Type Beat' in title")

        # ── Step 3: Navigate to each edit page and rename ──
        for i, beat_info in enumerate(beats_to_fix):
            edit_url = beat_info["edit_url"]
            old_t = beat_info["old_title"]
            new_t = beat_info["new_title"]
            p(f"  [{i+1}/{len(beats_to_fix)}] Renaming: '{old_t}' → '{new_t}'")

            try:
                driver.get(edit_url)
                time.sleep(4)

                # Find the name input (on edit page it's input[name='name'])
                name_el = None
                for sel in ["input[name='name']", "input[name='beats[0][name]']"]:
                    try:
                        name_el = driver.find_element(By.CSS_SELECTOR, sel)
                        if name_el.is_displayed():
                            break
                        name_el = None
                    except NoSuchElementException:
                        continue

                if not name_el:
                    # Fallback: find text input whose value contains "type beat"
                    inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text']")
                    for inp in inputs:
                        try:
                            val = inp.get_attribute("value") or ""
                            if "type beat" in val.lower() and inp.is_displayed():
                                name_el = inp
                                break
                        except StaleElementReferenceException:
                            continue

                if not name_el:
                    p(f"    [WARN] Could not find name input on {edit_url}")
                    continue

                # Clear and type new name
                if not safe_type(driver, name_el, new_t, clear=True):
                    p(f"    [WARN] Could not type into name field")
                    continue

                time.sleep(0.5)

                # Find visible Save button (btn btn-success pull-right)
                save_btn = None
                try:
                    candidates = driver.find_elements(By.CSS_SELECTOR,
                        "#beats-edit-form button[type='submit']"
                    )
                    for btn in candidates:
                        if btn.is_displayed():
                            save_btn = btn
                            break
                except NoSuchElementException:
                    pass

                if not save_btn:
                    save_btn = find_button_by_text(driver, ["Save", "Update"])

                if save_btn:
                    try:
                        driver.execute_script("arguments[0].scrollIntoView(true);", save_btn)
                        time.sleep(0.3)
                        save_btn.click()
                    except ElementClickInterceptedException:
                        driver.execute_script("arguments[0].click();", save_btn)
                    time.sleep(3)

                    slug = _re.sub(r'[^a-z0-9]+', '-', new_t.lower()).strip('-')
                    renamed[slug] = {"old_title": old_t, "new_title": new_t}
                    p(f"    [+] Renamed successfully")
                else:
                    p(f"    [WARN] Could not find save button")

            except Exception as e:
                p(f"    [WARN] Failed to rename '{old_t}': {e}")

            # Small delay between edits to be polite
            time.sleep(1)

        p(f"\n  [COMPLETE] Renamed {len(renamed)} beat(s) on Airbit")
        return renamed

    finally:
        if close_driver:
            driver.quit()


# ═══════════════════════════════════════════════
#  Store Beat Scraper — get all listing URLs
# ═══════════════════════════════════════════════

def scrape_store_beats(driver=None) -> dict:
    """Scrape all beat listing URLs from the Airbit Infinity Store.

    Returns dict: { "beat-slug": {"title": "...", "url": "https://..."}, ... }
    """
    close_driver = False
    if driver is None:
        driver = launch_browser()
        close_driver = True

    try:
        p("[~] Scraping Airbit Infinity Store for beat URLs...")
        driver.get(f"{AIRBIT_STORE_URL}beats")
        time.sleep(10)
        dismiss_cookie_banner(driver)

        # Scroll to load all beats (infinite scroll / lazy load)
        last_height = driver.execute_script("return document.body.scrollHeight")
        scroll_attempts = 0
        while scroll_attempts < 30:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                scroll_attempts += 1
                if scroll_attempts >= 3:
                    break
            else:
                scroll_attempts = 0
            last_height = new_height

        # Collect all beat links
        links = driver.find_elements(By.TAG_NAME, "a")
        beats = {}
        base = AIRBIT_STORE_URL.rstrip("/")
        for a in links:
            try:
                href = a.get_attribute("href") or ""
                if not href or "/beats/" not in href:
                    continue
                if href.endswith("/beats") or href.endswith("/beats/"):
                    continue
                if "#" in href.split("/beats/")[-1]:
                    continue
                # Extract slug
                slug = href.split("/beats/")[-1].strip("/")
                if not slug or slug in beats:
                    continue
                # Get title from parent or nearby elements
                title = ""
                try:
                    parent = a.find_element(By.XPATH, "..")
                    title = a.text.strip() or parent.text.strip().split("\n")[0]
                except Exception:
                    title = a.text.strip()
                beats[slug] = {
                    "title": title[:100] if title else slug,
                    "url": href if href.startswith("http") else f"{base}/beats/{slug}",
                }
            except Exception:
                continue

        p(f"  [+] Found {len(beats)} beat listings on Airbit store")
        return beats

    finally:
        if close_driver:
            driver.quit()


def match_store_to_stems(store_beats: dict, stems: list) -> dict:
    """Match Airbit store beat slugs to local stems.

    Returns dict: { stem: {"url": "...", "slug": "...", "title": "..."} }
    """
    import re as _re

    def _normalize(s: str) -> str:
        """Normalize a string for fuzzy matching."""
        s = s.lower().strip()
        # Remove common prefixes from Airbit titles and slugs
        # Space-based (from titles): 'BiggKutt8 Type Beat - "Army"'
        # Hyphen-based (from slugs): 'biggkutt8-type-beat-army'
        for prefix in [
            "biggkutt8 type beat - ", "sexyy red type beat - ",
            "glokk40spaz type beat - ", "glorilla type beat - ",
            "babyxsosa type beat - ", "sukihana type beat - ",
            "biggkutt8-type-beat-", "sexyy-red-type-beat-",
            "glokk40spaz-type-beat-", "glorilla-type-beat-",
            "babyxsosa-type-beat-", "sukihana-type-beat-",
        ]:
            if s.startswith(prefix):
                s = s[len(prefix):]
        # Also try generic "type beat" removal via regex
        s = _re.sub(r'^.+?\btype\s*beat\s*[-–]\s*', '', s)
        # Remove quotes, punctuation
        s = s.replace('"', '').replace("'", "").replace("!", "").replace("?", "")
        s = s.replace('\u201c', '').replace('\u201d', '')
        # Replace hyphens and spaces with underscores
        s = _re.sub(r'[\s\-]+', '_', s)
        # Remove non-alphanumeric (except underscores)
        s = _re.sub(r'[^a-z0-9_]', '', s)
        return s.strip("_")

    # Build lookup from normalized title → store entry
    store_lookup = {}
    for slug, info in store_beats.items():
        norm_slug = _normalize(slug)
        norm_title = _normalize(info["title"])
        store_lookup[norm_slug] = {"slug": slug, **info}
        if norm_title and norm_title != norm_slug:
            store_lookup[norm_title] = {"slug": slug, **info}

    matches = {}
    for stem in stems:
        norm = _normalize(stem)
        if norm in store_lookup:
            matches[stem] = store_lookup[norm]
            continue
        # Try partial match — require the shorter string to be a word-boundary
        # match (delimited by underscores) within the longer string, and ≥60%
        # of the longer string length. This prevents "hit" matching "dawg_shit",
        # "time" matching "game_time", etc.
        best_match = None
        best_score = 0
        for key, entry in store_lookup.items():
            if not key or not norm:
                continue
            shorter, longer = (norm, key) if len(norm) <= len(key) else (key, norm)
            if len(shorter) < 3:
                continue
            # Check if shorter is a complete word-boundary segment in longer
            # e.g. "master_plan" matches in "biggkutt8_master_plan" but
            # "hit" does NOT match in "dawg_shit"
            is_word_match = (
                longer == shorter or
                longer.startswith(shorter + "_") or
                longer.endswith("_" + shorter) or
                f"_{shorter}_" in longer
            )
            if is_word_match:
                ratio = len(shorter) / len(longer)
                if ratio >= 0.5 and ratio > best_score:
                    best_match = entry
                    best_score = ratio
        if best_match and stem not in matches:
            matches[stem] = best_match

    return matches


def sync_store_links() -> dict:
    """Scrape Airbit store, match to local stems, update store_uploads_log.json.

    Returns dict of updated stems with their URLs.
    """
    driver = launch_browser()
    try:
        store_beats = scrape_store_beats(driver)
    finally:
        driver.quit()

    if not store_beats:
        p("[WARN] No beats found on Airbit store")
        return {}

    # Get all local stems
    all_stems = [f.stem for f in sorted(BEATS_DIR.rglob("*.mp3"))]

    # Match
    matches = match_store_to_stems(store_beats, all_stems)
    p(f"  [+] Matched {len(matches)}/{len(all_stems)} stems to Airbit listings")

    # Update store_uploads_log.json
    store_log = json.loads(STORE_LOG_FILE.read_text()) if STORE_LOG_FILE.exists() else {}
    updated = 0
    for stem, info in matches.items():
        if stem not in store_log:
            store_log[stem] = {}
        if not isinstance(store_log[stem], dict):
            store_log[stem] = {}
        old_url = store_log[stem].get("airbit", {}).get("url", "")
        new_url = info["url"]
        if old_url != new_url:
            store_log[stem]["airbit"] = {
                "listing_id": info.get("slug", ""),
                "uploaded_at": store_log[stem].get("airbit", {}).get("uploaded_at", datetime.now().isoformat()),
                "url": new_url,
            }
            updated += 1

    STORE_LOG_FILE.write_text(json.dumps(store_log, indent=2))
    p(f"  [+] Updated {updated} entries in store_uploads_log.json")

    # Show unmatched
    unmatched_store = [s for s in store_beats if s not in [m.get("slug", "") for m in matches.values()]]
    unmatched_stems = [s for s in all_stems if s not in matches]
    if unmatched_store:
        p(f"  [INFO] {len(unmatched_store)} Airbit beats not matched to local stems")
    if unmatched_stems and len(unmatched_stems) < 20:
        p(f"  [INFO] Unmatched local stems: {', '.join(unmatched_stems[:10])}")

    return matches


# ═══════════════════════════════════════════════
#  Main Entry Point
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Upload beats to Airbit store (Selenium automation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
First-time setup:
  1. python airbit_upload.py --login      # Log in + calibrate selectors
  2. python airbit_upload.py --only army  # Test with 1 beat
  3. python airbit_upload.py              # Upload everything
        """
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview uploads without actually uploading")
    parser.add_argument("--only", type=str, default=None,
                        help='Comma-separated beat stems: --only "army,bappin"')
    parser.add_argument("--skip-uploaded", type=str, default="true",
                        help="Skip beats already in upload log (default: true)")
    parser.add_argument("--login", action="store_true",
                        help="Open browser for manual login + calibrate selectors")
    parser.add_argument("--discover", action="store_true",
                        help="Dump all upload page elements for debugging")
    parser.add_argument("--recalibrate", action="store_true",
                        help="Re-calibrate selectors (ignoring saved ones)")
    parser.add_argument("--delay", type=int, default=5,
                        help="Seconds between uploads (default: 5)")
    parser.add_argument("--sync-links", action="store_true",
                        help="Scrape Airbit store to get per-beat URLs and update store_uploads_log.json")
    parser.add_argument("--fix-titles", action="store_true",
                        help="Rename beats on Airbit that have 'Type Beat' in title to just the beat name")
    parser.add_argument("--fetch-short-links", action="store_true",
                        help="Visit each beat's edit page on Airbit and capture air.bi short links into metadata JSON")
    parser.add_argument("--verify", action="store_true",
                        help="Verify existing Airbit listings play the correct audio (use with --only)")
    args = parser.parse_args()

    # ── Verify mode ──
    if args.verify:
        stems_to_check = get_beats(args.only)
        if not stems_to_check:
            p("[ERROR] No beats to verify")
            sys.exit(1)
        driver = launch_browser()
        bad = []
        try:
            if not ensure_logged_in(driver):
                p("[ERROR] Could not log in to Airbit")
                sys.exit(1)
            for stem in stems_to_check:
                meta = load_metadata(stem)
                beat_title = meta.get("beat_name", stem.replace("_", " ").title())
                ok, vmsg = verify_listing_audio(driver, stem, beat_title)
                p(f"  [{'OK' if ok else 'BAD'}] {stem}: {vmsg}")
                if not ok:
                    bad.append(stem)
        finally:
            driver.quit()
        if bad:
            p(f"\n[MISMATCH] {len(bad)} listing(s) with wrong audio: {', '.join(bad)}")
            sys.exit(1)
        p(f"\n[COMPLETE] All {len(stems_to_check)} listing(s) verified — audio matches.")
        return

    # ── Fix titles mode ──
    if args.fix_titles:
        renamed = fix_airbit_titles()
        if renamed:
            p(f"\n[COMPLETE] Renamed {len(renamed)} beat(s) on Airbit.")
        else:
            p("\n[OK] No beats needed renaming.")
        return

    # ── Sync links mode ──
    if args.sync_links:
        matches = sync_store_links()
        if matches:
            p(f"\n[COMPLETE] {len(matches)} beat links synced. Run 'python upload.py --fix-descriptions' to push to YouTube.")
        return

    # ── Fetch short links mode ──
    if args.fetch_short_links:
        stems_to_check = get_beats(args.only)
        # Filter to only beats that don't already have an airbit_url
        missing = []
        for stem in stems_to_check:
            meta_path = META_DIR / f"{stem}.json"
            if meta_path.exists():
                try:
                    _m = json.loads(meta_path.read_text())
                    if not _m.get("airbit_url"):
                        missing.append(stem)
                except Exception:
                    missing.append(stem)
            else:
                missing.append(stem)

        if not missing:
            p("[OK] All beats already have airbit_url in metadata. Nothing to do.")
            p("     Use --skip-uploaded false to re-fetch all.")
            return

        p(f"[~] Fetching air.bi short links for {len(missing)} beat(s)...")
        driver = launch_browser()
        try:
            if not ensure_logged_in(driver):
                p("[ERROR] Could not log in to Airbit")
                driver.quit()
                sys.exit(1)

            # Load beats list once, then iterate
            p("[~] Loading beats management list...")
            driver.get("https://app.airbit.com/beats")
            time.sleep(6)
            dismiss_cookie_banner(driver)
            for _ in range(8):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.5)

            fetched = 0
            for stem in missing:
                meta = load_metadata(stem)
                beat_title = meta.get("beat_name", stem.replace("_", " ").title())
                p(f"\n  [{stem}] {beat_title}")
                short_link = _capture_airbit_short_link(driver, beat_title, stem)
                if short_link:
                    _save_airbit_url_to_metadata(stem, short_link)
                    fetched += 1
                else:
                    p(f"  [INFO] No short link found for {stem}")

            p(f"\n[COMPLETE] Fetched {fetched}/{len(missing)} air.bi short links.")
            p("  Run 'python upload.py --fix-descriptions' to push updated links to YouTube.")
        finally:
            driver.quit()
        return

    skip_uploaded = args.skip_uploaded.lower() != "false"

    # ── Get beats to upload ──
    beats = get_beats(args.only)
    if not beats and not args.login and not args.discover:
        p("[ERROR] No beats found to upload")
        sys.exit(1)

    # ── Load upload log ──
    log = load_log()

    # ── Filter already uploaded ──
    if skip_uploaded:
        pending = [b for b in beats if b not in log]
    else:
        pending = beats

    p("=" * 60)
    p("  Airbit Beat Upload Bot")
    p("=" * 60)

    if beats:
        p(f"\n  Total beats:      {len(beats)}")
        p(f"  Already uploaded:  {len(beats) - len(pending)}")
        p(f"  To upload:         {len(pending)}")

    # ═══ Dry Run ═══
    if args.dry_run:
        p(f"\n  [DRY RUN] Preview mode\n")
        for stem in pending:
            meta = load_metadata(stem)
            title = meta.get("title", stem)
            bpm = meta.get("bpm", "?")
            key_sig = meta.get("key", "?")
            genre = infer_genre(meta)
            moods = infer_moods(meta)
            tags = meta.get("tags", [])[:10]
            _beat_matches = list(BEATS_DIR.rglob(f"{stem}.mp3"))
            beat_file = _beat_matches[0] if _beat_matches else BEATS_DIR / f"{stem}.mp3"
            size_mb = beat_file.stat().st_size / (1024 * 1024) if beat_file.exists() else 0

            p(f"\n  [{stem}]")
            p(f"    Title:  {title}")
            p(f"    File:   {stem}.mp3 ({size_mb:.1f} MB)")
            p(f"    BPM:    {bpm} | Key: {key_sig}")
            p(f"    Genre:  {genre}")
            p(f"    Moods:  {', '.join(moods)}")
            p(f"    Tags:   {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}")

        p(f"\n{'=' * 60}")
        p(f"  Dry run: {len(pending)} beats ready to upload")
        p(f"{'=' * 60}")
        return

    # ═══ Launch Browser ═══
    p("\n[~] Launching Chrome (close other Chrome windows first)...")
    driver = launch_browser()

    try:
        # ── Login ──
        if not ensure_logged_in(driver):
            p("[ERROR] Could not log in to Airbit")
            driver.quit()
            sys.exit(1)

        # ── Calibrate selectors ──
        calibrated = {}
        if not args.recalibrate:
            calibrated = load_selectors()

        if not calibrated or args.recalibrate or args.login:
            calibrated = calibrate_selectors(driver)

        # ── Login-only mode ──
        if args.login:
            p("\n[OK] Login + calibration complete.")
            p("  Session is saved — future runs won't need login.")
            p("  Selectors saved — the bot knows where the form fields are.")
            p("\n  Next steps:")
            p("    python airbit_upload.py --only army   # Test 1 beat")
            p("    python airbit_upload.py               # Upload all")
            try:
                p("\nPress Enter to close browser...")
                input()
            except EOFError:
                pass
            driver.quit()
            return

        # ── Discover mode ──
        if args.discover:
            discover_upload_page(driver)
            try:
                p("\nPress Enter to close browser...")
                input()
            except EOFError:
                pass
            driver.quit()
            return

        # ═══ Upload Beats ═══
        if not pending:
            p("\n[OK] All beats already uploaded! Nothing to do.")
            driver.quit()
            return

        p(f"\n--- Uploading {len(pending)} beats ---\n")
        success = 0
        fail = 0
        failed_stems = []

        for i, stem in enumerate(pending):
            meta = load_metadata(stem)
            # Signal: [UPLOAD] stem (idx/total) — parsed by Telegram bot
            p(f"[UPLOAD] {stem} ({i + 1}/{len(pending)})")
            p(f"  {'─' * 40}")

            result = upload_beat(driver, stem, meta, calibrated)
            if result:
                # ── MANDATORY POST-UPLOAD AUDIO VERIFICATION ──
                # Download the MP3 Airbit actually stored and compare to the
                # local beat. A mismatch means the wrong audio got published
                # (stale-draft bug) — treat as a failed upload, don't record
                # the URL anywhere so the YT funnel never points at it.
                beat_title = meta.get("beat_name", stem.replace("_", " ").title())
                ok, vmsg = verify_listing_audio(driver, stem, beat_title)
                p(f"  [VERIFY] {vmsg}")
                if not ok:
                    fail += 1
                    failed_stems.append(stem)
                    p(f"[FAIL] {stem}: AUDIO VERIFICATION FAILED — the Airbit listing "
                      "does NOT play this beat. Delete the listing on Airbit and re-run.")
                    if i < len(pending) - 1:
                        time.sleep(args.delay)
                    continue
                listing_url = result if isinstance(result, str) else ""
                log[stem] = {
                    "title": meta.get("beat_name", stem),
                    "uploadedAt": datetime.now().isoformat(),
                    "bpm": meta.get("bpm"),
                    "genre": infer_genre(meta),
                    "url": listing_url,
                }
                save_log(log)
                record_store_log(stem, listing_url)  # Sync to backend store log
                # Also save any newly-discovered selectors
                save_selectors(calibrated)
                success += 1
                # Signal: [DONE] stem — parsed by Telegram bot
                p(f"[DONE] {stem}")
            else:
                fail += 1
                failed_stems.append(stem)
                # Signal: [FAIL] stem — parsed by Telegram bot
                p(f"[FAIL] {stem}: upload failed")

            # Rate limiting
            if i < len(pending) - 1:
                p(f"\n  [~] Waiting {args.delay}s before next upload...")
                time.sleep(args.delay)

        # ── Summary ──
        # Signal: [COMPLETE] — parsed by Telegram bot
        p(f"[COMPLETE] {success}/{len(pending)} uploaded successfully")
        p(f"\n{'=' * 60}")
        p(f"  Upload Complete")
        p(f"{'=' * 60}")
        p(f"  Succeeded: {success}")
        p(f"  Failed:    {fail}")
        if failed_stems:
            p(f"  Failed beats: {', '.join(failed_stems)}")
        p(f"  Log: {LOG_FILE}")
        p(f"{'=' * 60}")
        if fail:
            # Non-zero exit so upload.py doesn't report "[AIRBIT] Upload
            # complete ✓" when every beat actually failed/aborted.
            try:
                driver.quit()
            except Exception:
                pass
            sys.exit(1)

    except KeyboardInterrupt:
        p("\n\n[!] Interrupted by user")
        p(f"  Progress saved: {len(log)} beats in upload log")

    finally:
        p("\n[~] Closing browser...")
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
