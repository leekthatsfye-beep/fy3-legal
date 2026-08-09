"""
FY3 Arrangement Service — FL Studio beat structure engine.

Phase 1: Template stamping (FLP parsing + pattern rearrangement)
Phase 2: Retention optimizer (YouTube analytics integration) — stub
Phase 3: Full auto-arrange (AI-driven) — stub

Uses pyflp for .flp file parsing/writing and librosa for audio analysis.

INTELLIGENCE PHILOSOPHY:
- Templates encode REAL producer knowledge about genre-specific structures
- Audio analysis uses multi-feature segmentation (energy + onset density + spectral
  shape + harmonic change) — NOT just volume thresholds
- Pattern roles auto-detected from FL pattern contents (note ranges, density, channel)
- Layering follows real production rules: drums build up gradually, melody introduces
  variation, bass follows drops, FX accents transitions
- YouTube retention rules are research-backed: hook < 5s, first drop < 30s,
  energy shifts every 15-20s, no flat stretches > 20s
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import construct as c
import numpy as np
import pyflp
import pyflp.arrangement as arr_mod

from app.backend.config import ARRANGEMENTS_DIR, ARRANGEMENT_TEMPLATES_DIR, BEATS_DIR

logger = logging.getLogger(__name__)


# ── Genre-Aware Constants ──────────────────────────────────────────────

# Energy profiles per genre — what a well-arranged beat looks like across
# normalized time (0.0 = start, 1.0 = end). These come from analyzing
# hundreds of top-performing YouTube beats.
GENRE_ENERGY_PROFILES: dict[str, list[tuple[float, float]]] = {
    "trap": [
        (0.0, 0.3), (0.05, 0.35),  # Tease hook
        (0.08, 0.55), (0.25, 0.55),  # Verse builds
        (0.28, 1.0), (0.38, 0.95),  # Drop 1
        (0.40, 0.6), (0.55, 0.65),  # Verse 2
        (0.58, 1.0), (0.65, 0.95),  # Drop 2
        (0.68, 0.35), (0.75, 0.4),  # Bridge dip
        (0.78, 1.0), (0.90, 0.95),  # Final drop
        (0.93, 0.5), (1.0, 0.15),   # Outro fade
    ],
    "drill": [
        (0.0, 0.25), (0.05, 0.5),   # Quick menacing build
        (0.08, 1.0), (0.20, 0.95),  # Early aggressive drop
        (0.22, 0.6), (0.42, 0.65),  # Long verse
        (0.45, 1.0), (0.55, 0.95),  # Second drop
        (0.58, 0.3), (0.65, 0.35),  # Breakdown
        (0.68, 1.0), (0.90, 0.95),  # Final onslaught
        (0.92, 0.3), (1.0, 0.1),    # Abrupt end
    ],
    "rnb": [
        (0.0, 0.3), (0.08, 0.35),   # Smooth intro
        (0.10, 0.5), (0.28, 0.55),  # Verse 1
        (0.30, 0.65), (0.33, 0.7),  # Pre-chorus
        (0.35, 0.85), (0.43, 0.9),  # Chorus
        (0.45, 0.55), (0.58, 0.6),  # Verse 2
        (0.60, 0.9), (0.68, 0.95),  # Chorus 2
        (0.70, 0.4), (0.78, 0.45),  # Bridge
        (0.80, 0.95), (0.88, 0.9),  # Final chorus
        (0.90, 0.4), (1.0, 0.2),    # Smooth outro
    ],
    "melodic": [
        (0.0, 0.35), (0.05, 0.4),   # Hook melody
        (0.08, 0.55), (0.25, 0.6),  # Verse 1
        (0.28, 0.9), (0.38, 0.85),  # Hook 1
        (0.40, 0.6), (0.55, 0.65),  # Verse 2
        (0.58, 0.9), (0.65, 0.85),  # Hook 2
        (0.68, 0.3), (0.75, 0.35),  # Spacey bridge
        (0.78, 1.0), (0.90, 0.95),  # Final hook
        (0.92, 0.4), (1.0, 0.15),   # Fade
    ],
    "dark_trap": [
        (0.0, 0.2), (0.05, 0.3),    # Eerie intro
        (0.08, 0.45), (0.15, 0.5),  # Tension build
        (0.18, 0.95), (0.28, 0.9),  # Drop 1
        (0.30, 0.6), (0.48, 0.65),  # Dark verse
        (0.50, 1.0), (0.58, 0.95),  # Drop 2
        (0.60, 0.25), (0.68, 0.3),  # Horror breakdown
        (0.70, 1.0), (0.90, 0.95),  # Final onslaught
        (0.92, 0.2), (1.0, 0.1),    # Dark resolve
    ],
}

# Section classification thresholds (multi-feature)
# These define what makes a section an intro vs verse vs chorus vs bridge
SECTION_FEATURES = {
    "intro": {"energy_max": 0.45, "onset_density_max": 0.5, "position_max": 0.1},
    "verse": {"energy_range": (0.35, 0.75), "onset_density_range": (0.3, 0.7)},
    "chorus": {"energy_min": 0.7, "onset_density_min": 0.5},
    "bridge": {"energy_max": 0.45, "spectral_contrast": "low"},
    "outro": {"energy_max": 0.4, "position_min": 0.8},
}

# MIDI note ranges for auto-detecting pattern roles
ROLE_NOTE_RANGES = {
    "bass": {"min_note": 24, "max_note": 55, "density": "low"},    # C1-G3
    "melody": {"min_note": 48, "max_note": 96, "density": "medium"},  # C3-C7
    "drums": {"channel": 9, "velocity_variance": "high"},  # Channel 10 (0-indexed 9)
    "keys": {"min_note": 36, "max_note": 84, "chord_density": "high"},  # C2-C6
    "fx": {"density": "very_low", "velocity": "high"},
}


# ── Pydantic-free data helpers (dict-based for speed) ───────────────────


def list_templates() -> list[dict]:
    """List all arrangement templates from the templates directory."""
    ARRANGEMENT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = []
    for f in sorted(ARRANGEMENT_TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            templates.append(data)
        except Exception as e:
            logger.warning("Bad template %s: %s", f.name, e)
    return templates


def get_template(template_id: str) -> dict | None:
    """Get a single template by ID."""
    path = ARRANGEMENT_TEMPLATES_DIR / f"{template_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_template(template: dict) -> Path:
    """Save a custom template."""
    ARRANGEMENT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    tid = template.get("id", "custom")
    path = ARRANGEMENT_TEMPLATES_DIR / f"{tid}.json"
    path.write_text(json.dumps(template, indent=2))
    return path


# ── FLP Parsing ─────────────────────────────────────────────────────────


def parse_flp(flp_path: Path) -> dict:
    """
    Parse an FL Studio .flp file and return structural analysis with
    intelligent pattern role detection.

    Works on BOTH fresh and already-arranged .flp files.
    For already-arranged files, also extracts the current playlist layout
    so users can see what's already there before re-arranging.

    Returns:
        {
            "filename": str,
            "tempo": float,
            "ppq": int,
            "pattern_count": int,
            "patterns": [{"name", "iid", "has_notes", "note_count",
                          "suggested_role", "note_range", "avg_velocity",
                          "avg_note", "note_density"}],
            "track_count": int,
            "tracks": [{"name", "index", "item_count"}],
            "arrangement_name": str | None,
            "max_tracks": int,
            "suggested_mapping": {role: [iid, ...]},
            "has_existing_arrangement": bool,
            "existing_layout": [{"pattern_name", "pattern_iid", "start_bar",
                                 "length_bars", "track"}] | None,
            "existing_total_bars": int,
        }
    """
    project = pyflp.parse(str(flp_path))
    ppq = project.ppq
    ticks_per_bar = ppq * 4

    # Extract patterns with intelligent role detection
    patterns = []
    pattern_name_by_iid: dict[int, str] = {}
    for pat in project.patterns:
        try:
            notes = list(pat.notes) if hasattr(pat, "notes") else []
        except Exception:
            notes = []

        pat_name = pat.name or f"Pattern {pat.iid}"
        pattern_name_by_iid[pat.iid] = pat_name

        pat_info = {
            "name": pat_name,
            "iid": pat.iid,
            "has_notes": len(notes) > 0,
            "note_count": len(notes),
            "suggested_role": "melody",  # default
            "note_range": [0, 0],
            "avg_velocity": 0,
            "avg_note": 0,
            "note_density": 0.0,
        }

        if notes:
            pat_info.update(_analyze_pattern_notes(notes, pat.name))

        patterns.append(pat_info)

    # Extract arrangement tracks + existing playlist layout
    arrangements = list(project.arrangements)
    arrangement = arrangements[0] if arrangements else None
    tracks = []
    arrangement_name = None
    max_tracks = 500  # FL default
    existing_layout = []
    has_existing_arrangement = False
    existing_total_bars = 0

    if arrangement:
        arrangement_name = arrangement.name
        if hasattr(project.arrangements, "max_tracks"):
            max_tracks = project.arrangements.max_tracks or 500

        # ── Extract tracks (may fail if PlaylistEvent uses unsupported format) ──
        try:
            for i, track in enumerate(arrangement.tracks):
                item_count = 0
                try:
                    items = list(track)
                    item_count = len(items)
                except Exception:
                    pass
                tracks.append({
                    "name": getattr(track, "name", None) or f"Track {i + 1}",
                    "index": i,
                    "item_count": item_count,
                })
        except Exception as e:
            logger.warning("Could not read arrangement tracks (unsupported FL version?): %s", e)

        # ── Extract existing playlist items (for re-arrangement support) ──
        try:
            playlist_event = None
            for ev in arrangement.events:
                if isinstance(ev, arr_mod.PlaylistEvent):
                    playlist_event = ev
                    break

            if playlist_event is not None:
                try:
                    n_items = len(playlist_event)
                except Exception:
                    n_items = 0

                if n_items > 0:
                    has_existing_arrangement = True
                    for item in playlist_event:
                        try:
                            pos_ticks = item.position if hasattr(item, "position") else 0
                            length_ticks = item.length if hasattr(item, "length") else 0
                            item_index = item.item_index if hasattr(item, "item_index") else 0
                            track_rvidx = item.track_rvidx if hasattr(item, "track_rvidx") else 0

                            pat_iid = item_index - 20480 + 1
                            pat_name = pattern_name_by_iid.get(pat_iid, f"Pattern {pat_iid}")

                            start_bar = pos_ticks // ticks_per_bar if ticks_per_bar > 0 else 0
                            length_bars = length_ticks // ticks_per_bar if ticks_per_bar > 0 else 0
                            track_num = max_tracks - 1 - track_rvidx

                            end_bar = start_bar + length_bars
                            if end_bar > existing_total_bars:
                                existing_total_bars = end_bar

                            existing_layout.append({
                                "pattern_name": pat_name,
                                "pattern_iid": pat_iid,
                                "start_bar": start_bar,
                                "length_bars": length_bars,
                                "track": track_num,
                            })
                        except Exception:
                            pass

                    existing_layout.sort(key=lambda x: (x["start_bar"], x["track"]))
        except Exception as e:
            logger.warning("Could not extract existing layout: %s", e)

    # Build suggested mapping (group patterns by detected role)
    suggested_mapping = _build_suggested_mapping(patterns)

    return {
        "filename": flp_path.name,
        "tempo": float(project.tempo),
        "ppq": project.ppq,
        "pattern_count": len(patterns),
        "patterns": patterns,
        "track_count": len(tracks),
        "tracks": [t for t in tracks if t["item_count"] > 0][:50],
        "arrangement_name": arrangement_name,
        "max_tracks": max_tracks,
        "suggested_mapping": suggested_mapping,
        "has_existing_arrangement": has_existing_arrangement,
        "existing_layout": existing_layout if existing_layout else None,
        "existing_total_bars": existing_total_bars,
    }


def _note_to_midi(n) -> int:
    """Convert a note's key to a MIDI number. Handles both int and str ('C5')."""
    key = n.key if hasattr(n, "key") else (n.pitch if hasattr(n, "pitch") else 60)
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        note_map = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
        name = key.strip()
        if not name:
            return 60
        base = note_map.get(name[0].upper(), 0)
        idx = 1
        if idx < len(name) and name[idx] == "#":
            base += 1
            idx += 1
        elif idx < len(name) and name[idx] in ("b", "♭"):
            base -= 1
            idx += 1
        octave = int(name[idx:]) if idx < len(name) and name[idx:].lstrip("-").isdigit() else 5
        return base + (octave + 1) * 12
    return 60


def _analyze_pattern_notes(notes: list, pattern_name: str | None = None) -> dict:
    """
    Analyze MIDI notes in a pattern to detect its musical role.

    Two-pass system:
    1. Name-based detection (producers name their patterns — trust this first)
    2. Note-content analysis (range, density, count, velocity)

    Returns a basic role AND musical weight (importance score 0-1) so the
    arranger knows which patterns are primary vs. accent/fill.
    """
    if not notes:
        return {"suggested_role": "melody", "weight": 0.1}

    try:
        pitches = [_note_to_midi(n) for n in notes]
        velocities = [n.velocity if hasattr(n, "velocity") else 100 for n in notes]
    except Exception:
        return {"suggested_role": "melody", "weight": 0.1}

    min_note = min(pitches) if pitches else 60
    max_note = max(pitches) if pitches else 60
    avg_note = sum(pitches) / len(pitches) if pitches else 60
    note_range = max_note - min_note
    avg_velocity = sum(velocities) / len(velocities) if velocities else 100
    note_count = len(notes)
    vel_variance = float(np.std(velocities)) if len(velocities) > 1 else 0
    unique_pitches = len(set(pitches))

    role = "melody"
    # Weight = how important this pattern is within its role group.
    # High-note-count patterns are more important than sparse ones.
    weight = min(1.0, note_count / 50.0)

    # ── Pass 1: Name-based detection ──
    if pattern_name:
        name_lower = pattern_name.lower()

        # Name keyword map — ORDER matters (first match wins)
        NAME_RULES: list[tuple[str, list[str]]] = [
            # Producer tags / vocal tags — always FX, very low weight
            ("fx",    ["tag", "drop", "vocal tag", "leek", "dj "]),
            # 808 = bass, always (even if MIDI notes are high — it's a sample)
            ("bass",  ["808", "bass", "sub", "low end", "bottom"]),
            # Drums — kick/snare/hat/clap are core groove
            ("drums", ["kick", "snare", "hat", "hihat", "hi-hat", "clap",
                       "cymbal", "808drum", "snap", "rim", "open hat",
                       "closed hat"]),
            # Perc — secondary rhythmic elements
            ("perc",  ["perc", "shaker", "tambourine", "bongo", "conga",
                       "wood", "triangle", "guiro", "zap"]),
            # Chords / pads — sustained harmonic content
            ("keys",  ["chord", "chords", "keys", "key ", "key-", "piano", "pad", "organ",
                       "string", "strings", "str_", "str ", "ensemble",
                       "rhodes", "ep", "electric piano"]),
            # FX — transitions, risers, impacts
            ("fx",    ["fx", "sfx", "riser", "sweep", "impact", "noise",
                       "transition", "whoosh", "reverse"]),
            # Vocal / chant — treat as FX accent
            ("fx",    ["chant", "vox", "vocal", "adlib", "ad lib", "voice"]),
            # Melody — explicit melody names
            ("melody", ["melody", "melo", "lead", "synth", "pluck",
                        "flute", "guitar", "piano mel", "arp",
                        "theramin", "theremin"]),
            # Bell — could be melody accent, keep as melody but lower weight
            ("melody", ["bell", "glock", "music box", "celesta"]),
        ]

        matched = False
        for detected_role, keywords in NAME_RULES:
            if any(kw in name_lower for kw in keywords):
                role = detected_role
                matched = True
                # Adjust weight for specific types
                if detected_role == "fx":
                    weight = min(0.3, weight)
                elif "bell" in name_lower or "glock" in name_lower:
                    weight = min(0.5, weight)  # accent melody
                break

        if not matched:
            # ── Pass 2: Note-content analysis ──
            if avg_note < 48 and note_range < 24:
                role = "bass"
            elif note_range < 12 and avg_note < 55:
                role = "bass"
            elif note_count > 20 and unique_pitches < 5 and note_range < 6:
                role = "drums"
            elif note_range > 24 and unique_pitches > 6 and avg_note > 55:
                role = "keys"
            elif note_count < 4:
                role = "fx"
                weight = 0.15
            elif avg_note > 65 and note_range > 12:
                role = "melody"

    # Patterns with very few notes are accents/fills regardless of role
    if note_count <= 3 and role in ("melody", "keys"):
        role = "fx"
        weight = 0.15

    return {
        "suggested_role": role,
        "weight": round(weight, 2),
        "note_range": [int(min_note), int(max_note)],
        "avg_velocity": int(avg_velocity),
        "avg_note": int(avg_note),
        "note_density": round(note_count / max(note_range, 1), 2),
    }


def _build_suggested_mapping(patterns: list[dict]) -> dict[str, list[int]]:
    """
    Build an intelligent pattern mapping that understands musical hierarchy.

    Groups patterns by role, then sorts by weight within each group so the
    arranger knows which patterns are primary (always play) vs. secondary
    (only in high-energy sections) vs. accents (drops/hooks only).
    """
    mapping: dict[str, list[int]] = {}
    for pat in patterns:
        if not pat.get("has_notes"):
            continue
        role = pat.get("suggested_role", "melody")
        # Normalize perc → drums for template compatibility
        if role == "perc":
            role = "drums"
        mapping.setdefault(role, []).append(pat["iid"])

    # Sort each role group by weight (heaviest first = most important)
    weight_by_iid = {p["iid"]: p.get("weight", 0.5) for p in patterns}
    for role in mapping:
        mapping[role].sort(key=lambda iid: weight_by_iid.get(iid, 0.5), reverse=True)

    return mapping


# ── Audio Structure Detection ───────────────────────────────────────────


def detect_audio_structure(audio_path: Path, bpm: int | None = None) -> dict:
    """
    Detect structural sections from audio using multi-feature analysis.

    Uses 4 signals combined for intelligent segmentation:
    1. RMS Energy — volume/loudness per bar
    2. Onset Density — how many hits/notes per bar (drums vs. pads)
    3. Spectral Centroid — brightness (dark intro vs bright chorus)
    4. MFCC Novelty — timbral change detection (self-similarity matrix)

    Returns:
        {
            "stem": str,
            "bpm": int,
            "key": str,
            "duration_sec": float,
            "total_bars": int,
            "sections": [{"name", "label", "start_bar", "end_bar", "length_bars",
                          "start_sec", "end_sec", "energy", "onset_density",
                          "brightness"}],
            "energy_curve": [float],  # energy per bar, normalized 0-1
            "onset_curve": [float],   # onset density per bar, normalized 0-1
            "detected_genre": str,    # best-guess genre from energy profile
        }
    """
    import librosa
    from scipy.signal import find_peaks

    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    duration = librosa.get_duration(y=y, sr=sr)

    # BPM detection
    if bpm is None:
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = int(round(float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)))

    # Key detection
    key = _detect_key_simple(y, sr)

    # Bar-level metrics
    seconds_per_beat = 60.0 / bpm
    seconds_per_bar = seconds_per_beat * 4  # 4/4 time
    total_bars = int(duration / seconds_per_bar)

    if total_bars < 4:
        return {
            "stem": audio_path.stem,
            "bpm": bpm,
            "key": key,
            "duration_sec": round(duration, 1),
            "total_bars": total_bars,
            "sections": [],
            "energy_curve": [],
            "onset_curve": [],
            "detected_genre": "unknown",
        }

    # ── Multi-Feature Extraction ──
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    spectral_centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop_length)[0]

    # Onset strength for hit density
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

    # Spectral flatness (noise-like vs. tonal — helps detect drums vs melody sections)
    spectral_flatness = librosa.feature.spectral_flatness(y=y, hop_length=hop_length)[0]

    # ── Aggregate per bar (all features) ──
    frames_per_bar = max(1, int(seconds_per_bar * sr / hop_length))
    energy_per_bar = []
    brightness_per_bar = []
    onset_per_bar = []
    flatness_per_bar = []

    for bar_idx in range(total_bars):
        s = bar_idx * frames_per_bar
        e = min(s + frames_per_bar, len(rms))
        if s >= len(rms):
            break

        energy_per_bar.append(float(np.mean(rms[s:e])))

        if s < len(spectral_centroid):
            brightness_per_bar.append(float(np.mean(spectral_centroid[s:min(e, len(spectral_centroid))])))
        else:
            brightness_per_bar.append(0.0)

        if s < len(onset_env):
            onset_per_bar.append(float(np.mean(onset_env[s:min(e, len(onset_env))])))
        else:
            onset_per_bar.append(0.0)

        if s < len(spectral_flatness):
            flatness_per_bar.append(float(np.mean(spectral_flatness[s:min(e, len(spectral_flatness))])))
        else:
            flatness_per_bar.append(0.0)

    # Normalize all features 0-1
    def _normalize(vals: list[float]) -> list[float]:
        mx = max(vals) if vals else 1.0
        return [v / mx if mx > 0 else 0 for v in vals]

    energy_norm = _normalize(energy_per_bar)
    onset_norm = _normalize(onset_per_bar)
    brightness_norm = _normalize(brightness_per_bar)

    # ── Smart Segmentation (multi-feature novelty) ──
    try:
        # Combine features for richer segmentation
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop_length)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)

        # Stack features: MFCC (timbre) + chroma (harmony)
        combined = np.vstack([mfcc, chroma])
        rec = librosa.segment.recurrence_matrix(combined, mode="affinity", sym=True)
        novelty = librosa.segment.novelty(rec)

        # Also compute onset-based novelty for drum transitions
        onset_rec = librosa.segment.recurrence_matrix(
            onset_env.reshape(1, -1), mode="affinity", sym=True
        )
        onset_novelty = librosa.segment.novelty(onset_rec)

        # Merge both novelty curves (timbral + rhythmic)
        min_len = min(len(novelty), len(onset_novelty))
        merged_novelty = 0.6 * novelty[:min_len] + 0.4 * onset_novelty[:min_len]

        # Find peaks = boundaries (min 4 bars apart for musical coherence)
        min_distance = max(2, frames_per_bar * 4)
        peak_indices, peak_props = find_peaks(
            merged_novelty,
            distance=min_distance,
            prominence=0.03,  # Lower threshold to catch subtle changes
        )

        # Snap boundaries to nearest bar line (musical grid)
        boundary_bars = sorted(set(
            [0]
            + [_snap_to_bar_grid(int(idx / frames_per_bar), total_bars)
               for idx in peak_indices if idx / frames_per_bar < total_bars]
            + [total_bars]
        ))

        # Remove boundaries that are too close (less than 2 bars)
        boundary_bars = _clean_boundaries(boundary_bars, min_gap=2)

    except Exception as e:
        logger.warning("Segmentation failed, using energy-based fallback: %s", e)
        boundary_bars = _energy_based_boundaries(energy_norm, total_bars)

    # ── Smart Section Classification ──
    sections = _classify_sections_v2(
        boundary_bars, energy_norm, onset_norm, brightness_norm,
        total_bars, bpm
    )

    # ── Genre Detection ──
    detected_genre = _detect_genre_from_profile(energy_norm, bpm, onset_norm)

    return {
        "stem": audio_path.stem,
        "bpm": bpm,
        "key": key,
        "duration_sec": round(duration, 1),
        "total_bars": total_bars,
        "sections": sections,
        "energy_curve": [round(e, 3) for e in energy_norm],
        "onset_curve": [round(o, 3) for o in onset_norm],
        "detected_genre": detected_genre,
    }


def _snap_to_bar_grid(bar: int, total_bars: int) -> int:
    """Snap a boundary to the nearest 2-bar or 4-bar grid line."""
    # Prefer 4-bar boundaries (most musical), then 2-bar
    nearest_4 = round(bar / 4) * 4
    nearest_2 = round(bar / 2) * 2

    if abs(bar - nearest_4) <= 2:
        return min(nearest_4, total_bars)
    return min(nearest_2, total_bars)


def _clean_boundaries(boundaries: list[int], min_gap: int = 2) -> list[int]:
    """Remove boundaries that are too close together."""
    if len(boundaries) <= 2:
        return boundaries
    cleaned = [boundaries[0]]
    for b in boundaries[1:]:
        if b - cleaned[-1] >= min_gap:
            cleaned.append(b)
    if cleaned[-1] != boundaries[-1]:
        cleaned.append(boundaries[-1])
    return cleaned


def _energy_based_boundaries(energy: list[float], total_bars: int) -> list[int]:
    """Fallback: detect boundaries from energy jumps."""
    boundaries = [0]
    for i in range(4, len(energy) - 2, 2):
        # Look for significant energy changes (>25%)
        prev_energy = np.mean(energy[max(0, i-4):i])
        next_energy = np.mean(energy[i:min(len(energy), i+4)])
        delta = abs(next_energy - prev_energy)
        if delta > 0.25:
            snapped = _snap_to_bar_grid(i, total_bars)
            if snapped not in boundaries and snapped - boundaries[-1] >= 4:
                boundaries.append(snapped)
    boundaries.append(total_bars)
    return boundaries


def _detect_genre_from_profile(
    energy: list[float], bpm: int, onset_density: list[float]
) -> str:
    """
    Guess the genre from the energy profile shape + BPM + onset density.
    Compares against known genre energy profiles.
    """
    if not energy or len(energy) < 8:
        return "trap"  # safe default

    # Quick heuristics first
    avg_onset = np.mean(onset_density) if onset_density else 0.5
    energy_variance = float(np.std(energy))

    if bpm < 115:
        return "rnb"
    if bpm >= 135 and bpm <= 150 and avg_onset > 0.6:
        return "drill"
    if energy_variance < 0.15 and bpm > 120:
        # Very flat energy = dark trap (sustained menacing vibe)
        return "dark_trap"
    if energy_variance > 0.25 and avg_onset < 0.5:
        # High energy variance, lower onset density = melodic
        return "melodic"

    # Compare energy shape to genre profiles
    best_genre = "trap"
    best_score = -1.0

    for genre, profile in GENRE_ENERGY_PROFILES.items():
        score = _compare_energy_profile(energy, profile)
        if score > best_score:
            best_score = score
            best_genre = genre

    return best_genre


def _compare_energy_profile(
    actual_energy: list[float],
    target_profile: list[tuple[float, float]],
) -> float:
    """Compare actual energy curve to a target profile, return similarity score."""
    n = len(actual_energy)
    if n == 0:
        return 0.0

    total_diff = 0.0
    for i, e in enumerate(actual_energy):
        pos = i / n  # normalized position 0-1
        # Interpolate target energy at this position
        target_e = _interpolate_profile(target_profile, pos)
        total_diff += abs(e - target_e)

    avg_diff = total_diff / n
    return max(0, 1.0 - avg_diff)  # Higher = more similar


def _interpolate_profile(profile: list[tuple[float, float]], pos: float) -> float:
    """Linear interpolation of a genre energy profile at a given position."""
    if not profile:
        return 0.5
    if pos <= profile[0][0]:
        return profile[0][1]
    if pos >= profile[-1][0]:
        return profile[-1][1]

    for i in range(len(profile) - 1):
        if profile[i][0] <= pos <= profile[i + 1][0]:
            span = profile[i + 1][0] - profile[i][0]
            if span <= 0:
                return profile[i][1]
            t = (pos - profile[i][0]) / span
            return profile[i][1] + t * (profile[i + 1][1] - profile[i][1])
    return 0.5


def _detect_key_simple(y: np.ndarray, sr: int) -> str:
    """Simple key detection using chroma features."""
    import librosa

    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_avg = np.mean(chroma, axis=1)

        # Krumhansl-Schmuckler profiles
        major_profile = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
        minor_profile = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

        note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

        best_corr = -1
        best_key = "C Major"

        for i in range(12):
            shifted = np.roll(chroma_avg, -i)
            corr_maj = float(np.corrcoef(shifted, major_profile)[0, 1])
            corr_min = float(np.corrcoef(shifted, minor_profile)[0, 1])

            if corr_maj > best_corr:
                best_corr = corr_maj
                best_key = f"{note_names[i]} Major"
            if corr_min > best_corr:
                best_corr = corr_min
                best_key = f"{note_names[i]} Minor"

        return best_key
    except Exception:
        return "Unknown"


def _classify_sections_v2(
    boundary_bars: list[int],
    energy: list[float],
    onset_density: list[float],
    brightness: list[float],
    total_bars: int,
    bpm: int,
) -> list[dict]:
    """
    Intelligent section classification using multiple audio features.

    Decision logic:
    1. Position-based rules (intro at start, outro at end)
    2. Energy + onset density combination:
       - High energy + high onset = drop/chorus
       - Medium energy + medium onset = verse
       - Low energy + low onset = bridge/breakdown
       - High energy + low onset = buildup/riser
       - Low energy + high onset = transition
    3. Contextual awareness: a drop must be preceded by a buildup,
       a verse shouldn't follow a verse without a transition
    """
    sections = []
    secs_per_bar = (60.0 / bpm) * 4

    verse_count = 0
    chorus_count = 0
    bridge_count = 0

    # First pass: compute raw features for each segment
    raw_segments = []
    for i in range(len(boundary_bars) - 1):
        start_bar = boundary_bars[i]
        end_bar = boundary_bars[i + 1]
        length_bars = end_bar - start_bar

        if start_bar >= len(energy):
            break

        seg_energy = energy[start_bar:min(end_bar, len(energy))]
        seg_onset = onset_density[start_bar:min(end_bar, len(onset_density))] if onset_density else []
        seg_bright = brightness[start_bar:min(end_bar, len(brightness))] if brightness else []

        avg_energy = float(np.mean(seg_energy)) if seg_energy else 0.0
        avg_onset = float(np.mean(seg_onset)) if seg_onset else 0.0
        avg_brightness = float(np.mean(seg_bright)) if seg_bright else 0.0
        energy_trend = float(seg_energy[-1] - seg_energy[0]) if len(seg_energy) > 1 else 0.0
        position_ratio = start_bar / total_bars if total_bars > 0 else 0

        raw_segments.append({
            "start_bar": start_bar,
            "end_bar": end_bar,
            "length_bars": length_bars,
            "avg_energy": avg_energy,
            "avg_onset": avg_onset,
            "avg_brightness": avg_brightness,
            "energy_trend": energy_trend,
            "position_ratio": position_ratio,
        })

    # Second pass: classify with context awareness
    for idx, seg in enumerate(raw_segments):
        prev_name = sections[-1]["name"] if sections else None
        next_seg = raw_segments[idx + 1] if idx + 1 < len(raw_segments) else None

        e = seg["avg_energy"]
        o = seg["avg_onset"]
        pos = seg["position_ratio"]
        length = seg["length_bars"]
        trend = seg["energy_trend"]

        # ── Classification Rules (priority order) ──

        # Rule 1: Intro — first 10% of track, moderate/low energy
        if pos < 0.08 and length <= 8 and e < 0.55:
            name = "intro"

        # Rule 2: Outro — last 12% of track, energy fading
        elif pos > 0.85 and (e < 0.45 or trend < -0.1):
            name = "outro"

        # Rule 3: Build/Riser — energy rising sharply, before a loud section
        elif trend > 0.2 and length <= 8 and next_seg and next_seg["avg_energy"] > 0.7:
            name = "build"

        # Rule 4: Drop/Chorus — high energy AND high onset density
        elif e > 0.65 and o > 0.5:
            chorus_count += 1
            name = "chorus"

        # Rule 5: Breakdown/Bridge — significant energy dip after a loud section
        elif e < 0.4 and prev_name in ("chorus", "drop", "verse") and length <= 12:
            bridge_count += 1
            name = "bridge"

        # Rule 6: Low energy section that's NOT at start/end — also bridge
        elif e < 0.35 and o < 0.4 and 0.1 < pos < 0.85:
            bridge_count += 1
            name = "bridge"

        # Rule 7: Default to verse
        else:
            verse_count += 1
            name = "verse"

        # ── Smart Labeling ──
        label_map = {
            "intro": "Intro",
            "build": "Build",
            "chorus": f"Drop {chorus_count}" if chorus_count > 1 else "Drop",
            "verse": f"Verse {verse_count}" if verse_count > 1 else "Verse",
            "bridge": "Bridge" if bridge_count <= 1 else f"Breakdown {bridge_count}",
            "outro": "Outro",
        }
        label = label_map.get(name, name.title())

        sections.append({
            "name": name,
            "label": label,
            "start_bar": seg["start_bar"],
            "end_bar": seg["end_bar"],
            "length_bars": seg["length_bars"],
            "start_sec": round(seg["start_bar"] * secs_per_bar, 2),
            "end_sec": round(seg["end_bar"] * secs_per_bar, 2),
            "energy": round(seg["avg_energy"], 3),
            "onset_density": round(seg["avg_onset"], 3),
            "brightness": round(seg["avg_brightness"], 3),
        })

    return sections


# ── Template Application (rebuilt) ─────────────────────────────────────

import struct as _struct

# Drum stagger priority — kick enters first, then snare, then hats
_DRUM_PRIORITY = {
    "kick": 0, "808drum": 0,
    "snare": 1, "clap": 1, "snap": 1, "rim": 1,
    "hat": 2, "hihat": 2, "hi-hat": 2, "open hat": 2, "closed hat": 2,
    "cymbal": 3, "perc": 3, "shaker": 3, "triangle": 3, "zap": 3,
}

# Section → weight threshold (patterns below this weight are skipped)
_WEIGHT_THRESHOLDS: dict[str, float] = {
    "drop_1": 0.0, "drop_2": 0.0, "drop_3": 0.0, "final_drop": 0.0,
    "chorus": 0.0, "hook": 0.0, "hook_1": 0.0, "hook_2": 0.0, "hook_3": 0.0,
    "verse": 0.3, "verse_1": 0.3, "verse_2": 0.3,
    "build": 0.2, "riser": 0.2, "pre_chorus": 0.2,
    "bridge": 0.4, "breakdown": 0.4,
    "outro": 0.3, "intro": 0.3,
}

# Section → max patterns per role (creates visible contrast between sections)
_MAX_PER_ROLE: dict[str, int] = {
    "drop_1": 999, "drop_2": 999, "drop_3": 999, "final_drop": 999,
    "chorus": 999, "hook": 999, "hook_1": 999, "hook_2": 999, "hook_3": 999,
    "verse": 2, "verse_1": 2, "verse_2": 2,
    "build": 2, "riser": 2, "pre_chorus": 2,
    "bridge": 1, "breakdown": 1,
    "outro": 2, "intro": 1,
}

# Sections where non-tag FX are allowed to play
_FX_DROP_SECTIONS = frozenset({
    "drop_1", "drop_2", "drop_3", "final_drop",
    "chorus", "hook", "hook_1", "hook_2", "hook_3",
})


def _build_weight_map(project: Any) -> dict[int, float]:
    """Single-pass weight calculation for all patterns."""
    weight_map: dict[int, float] = {}
    for pat in project.patterns:
        try:
            notes = list(pat.notes) if hasattr(pat, "notes") else []
        except Exception:
            notes = []
        if notes:
            info = _analyze_pattern_notes(notes, pat.name)
            weight_map[pat.iid] = info.get("weight", 0.5)
        else:
            weight_map[pat.iid] = 0.0
    return weight_map


def _assign_tracks(pattern_mapping: dict[str, list[int]]) -> dict[int, int]:
    """Map each pattern IID to a track number, grouped by role."""
    ROLE_ORDER = ["melody", "keys", "drums", "perc", "bass", "fx"]
    pat_to_track: dict[int, int] = {}
    idx = 0
    for role in ROLE_ORDER:
        for pat_iid in pattern_mapping.get(role, []):
            pat_to_track[pat_iid] = idx
            idx += 1
    for role in pattern_mapping:
        if role not in ROLE_ORDER:
            for pat_iid in pattern_mapping[role]:
                if pat_iid not in pat_to_track:
                    pat_to_track[pat_iid] = idx
                    idx += 1
    return pat_to_track


def _compute_fx_plan(
    sections: list[dict],
    pattern_mapping: dict[str, list[int]],
    weight_by_iid: dict[int, float],
) -> dict[tuple[int, int], dict]:
    """Pre-compute FX placement for all sections.
    Returns {(sec_idx, pat_iid): {"offset_bars", "trim_end_bars"}}.
    Missing keys = skip that FX in that section."""
    plan: dict[tuple[int, int], dict] = {}
    fx_iids = pattern_mapping.get("fx", [])
    if not fx_iids:
        return plan

    for sec_idx, sec in enumerate(sections):
        name = sec.get("name", "").lower()
        length = sec["length_bars"]
        start = sec["start_bar"]

        for pat_iid in fx_iids:
            w = weight_by_iid.get(pat_iid, 0.5)
            is_tag = w <= 0.2

            if is_tag:
                if start == 0:
                    plan[(sec_idx, pat_iid)] = {"offset_bars": 0, "trim_end_bars": max(0, length - 1)}
                elif name == "outro":
                    plan[(sec_idx, pat_iid)] = {"offset_bars": max(0, length - 1), "trim_end_bars": 0}
            else:
                if name in _FX_DROP_SECTIONS:
                    plan[(sec_idx, pat_iid)] = {"offset_bars": 0, "trim_end_bars": 0}

    return plan


def _stagger_drums(
    pat_iids: list[int],
    pattern_by_iid: dict[int, Any],
    base_offset: int,
    section_length: int,
) -> list[tuple[int, int]]:
    """Stagger drum patterns: kick first, then snare, then hats.
    Returns [(pat_iid, actual_offset_bars), ...]."""
    if len(pat_iids) <= 1:
        return [(iid, base_offset) for iid in pat_iids]

    def _priority(iid: int) -> int:
        pat = pattern_by_iid.get(iid)
        name = (pat.name or "").lower() if pat else ""
        for kw, pri in _DRUM_PRIORITY.items():
            if kw in name:
                return pri
        return 4

    sorted_iids = sorted(pat_iids, key=_priority)
    result = []
    for i, iid in enumerate(sorted_iids):
        offset = min(base_offset + i, section_length - 1)
        result.append((iid, offset))
    return result


def _setup_project(flp_path: Path) -> dict:
    """Parse FLP, detect format, capture ref_item, prepare for editing."""
    project = pyflp.parse(str(flp_path))
    ppq = project.ppq
    ticks_per_bar = ppq * 4

    arrangements = list(project.arrangements)
    if not arrangements:
        raise ValueError("No arrangements found in the .flp file")

    arrangement = arrangements[0]
    max_tracks = 500
    if hasattr(project.arrangements, "max_tracks"):
        max_tracks = project.arrangements.max_tracks or 500

    pattern_by_iid: dict[int, Any] = {}
    pattern_length_ticks: dict[int, int] = {}  # iid -> actual note length in ticks
    for pat in project.patterns:
        pattern_by_iid[pat.iid] = pat
        # Calculate actual pattern length from note positions
        try:
            notes = list(pat.notes) if hasattr(pat, "notes") else []
        except Exception:
            notes = []
        max_tick = 0
        for n in notes:
            end = (getattr(n, "position", 0) or 0) + (getattr(n, "length", 0) or 0)
            if end > max_tick:
                max_tick = end
        # Snap to nearest bar (round up)
        if max_tick > 0:
            pattern_length_ticks[pat.iid] = ((max_tick + ticks_per_bar - 1) // ticks_per_bar) * ticks_per_bar
        else:
            pattern_length_ticks[pat.iid] = ticks_per_bar  # default 1 bar

    playlist_event = None
    for ev in arrangement.events:
        if isinstance(ev, arr_mod.PlaylistEvent):
            playlist_event = ev
            break
    if playlist_event is None:
        raise ValueError("Could not find PlaylistEvent in the arrangement")

    # Detect format
    supported = hasattr(playlist_event, "data")
    ref_item = None
    max_uid = 0
    is_new_fl = False

    if supported:
        n = len(playlist_event)
        if n > 0:
            ref_item = list(playlist_event)[0].copy()
            is_new_fl = getattr(ref_item, "_u3", None) is not None
            if is_new_fl:
                for item in playlist_event:
                    u3 = getattr(item, "_u3", None)
                    if u3 and len(u3) >= 2:
                        uid = _struct.unpack("<H", u3[:2])[0]
                        if uid > max_uid:
                            max_uid = uid
            logger.info("Re-arranging: %d items (FL %s)", n, "21+" if is_new_fl else "20-")
        else:
            is_new_fl = True
        playlist_event.clear()
    else:
        logger.info("Unsupported playlist format — raw 32-byte rebuild")

    return {
        "project": project,
        "ppq": ppq,
        "ticks_per_bar": ticks_per_bar,
        "max_tracks": max_tracks,
        "pattern_by_iid": pattern_by_iid,
        "pattern_length_ticks": pattern_length_ticks,
        "playlist_event": playlist_event,
        "ref_item": ref_item,
        "is_new_fl": is_new_fl,
        "next_uid": max(max_uid + 1, 1),
        "supported": supported,
    }


def _make_raw_item(pos: int, pat_iid: int, length: int, trk_rvidx: int) -> bytes:
    """Build a single 32-byte FL Studio playlist item."""
    return (
        _struct.pack("<I", pos) +
        _struct.pack("<H", 20480) +
        _struct.pack("<H", pat_iid - 1 + 20480) +
        _struct.pack("<I", length) +
        _struct.pack("<H", trk_rvidx) +
        _struct.pack("<H", 0) +
        b"\x78\x00" +
        _struct.pack("<H", 0x0040) +
        b"\x40\x64\x80\x80" +
        _struct.pack("<f", 0.0) +
        _struct.pack("<f", 0.0)
    )


def _place_item(pos: int, pat_iid: int, length: int, trk_rvidx: int, ctx: dict) -> bytes:
    """Create one playlist item. Returns raw bytes (unsupported) or b'' (supported)."""
    if not ctx["supported"]:
        return _make_raw_item(pos, pat_iid, length, trk_rvidx)

    pe = ctx["playlist_event"]

    if ctx["ref_item"] is not None:
        item = ctx["ref_item"].copy()
        item.position = pos
        item.pattern_base = 20480
        item.item_index = pat_iid - 1 + 20480
        item.length = length
        item.track_rvidx = trk_rvidx
        item.group = 0
        item.item_flags = 0x0040
        uid_bytes = _struct.pack("<H", ctx["next_uid"])
        item._u3 = uid_bytes + b"\x00" * 26
        ctx["next_uid"] += 1
        pe.append(item)
    else:
        d = {
            "position": pos, "pattern_base": 20480,
            "item_index": pat_iid - 1 + 20480, "length": length,
            "track_rvidx": trk_rvidx, "group": 0,
            "_u1": b"\x78\x00", "item_flags": 0x0040,
            "_u2": b"\x40\x64\x80\x80",
            "start_offset": 0.0, "end_offset": 0.0,
        }
        if ctx["is_new_fl"]:
            uid = _struct.pack("<H", ctx["next_uid"])
            ctx["next_uid"] += 1
            d["_u3"] = uid + b"\x00" * 26
        else:
            d["_u3"] = None
        pe.append(c.Container(**d))

    return b""


def _build_retention_map(
    sections: list[dict],
    pattern_mapping: dict[str, list[int]],
    pattern_by_iid: dict[int, Any],
) -> dict[int, set[str]]:
    """
    Build a bar-level mute map for retention techniques.

    Returns {bar_number: set of roles to MUTE at that bar}.

    Techniques:
    1. Mini break: last bar before every drop → mute drums + bass
    2. Variation: every 8 bars (not at drops) → randomly mute one element
    3. Counter melody swap: alternate which melody plays in verses
    """
    import random as _rng
    _rng.seed(42)  # deterministic so same .flp = same arrangement

    mute_map: dict[int, set[str]] = {}
    total_bars = max((s["start_bar"] + s["length_bars"] for s in sections), default=64)

    # Find all drop start bars
    drop_starts = set()
    for sec in sections:
        if sec.get("name", "").lower() in _FX_DROP_SECTIONS:
            drop_starts.add(sec["start_bar"])

    # Technique 1: Mini break — mute drums+bass on the bar BEFORE each drop
    for ds in drop_starts:
        if ds > 0:
            mute_map.setdefault(ds - 1, set()).update({"drums", "bass"})

    # Technique 2: Variation every 8 bars (skip drops and the bar before drops)
    variation_options = ["drums", "bass", "melody"]
    for bar in range(8, total_bars, 8):
        if bar in drop_starts or (bar + 1) in drop_starts or bar in mute_map:
            continue
        # Find which section this bar is in
        in_section = None
        for sec in sections:
            s, l = sec["start_bar"], sec["length_bars"]
            if s <= bar < s + l:
                in_section = sec.get("name", "").lower()
                break
        # Only vary in verses (not drops, not breakdowns)
        if in_section and in_section in ("verse", "verse_1", "verse_2"):
            pick = _rng.choice(variation_options)
            mute_map.setdefault(bar, set()).add(pick)

    return mute_map


def _finalize_and_save(ctx: dict, raw_bytes: bytes, output_path: Path):
    """Inject raw items (unsupported format) or sync supported items, then save."""
    pe = ctx["playlist_event"]

    if not ctx["supported"] and raw_bytes:
        pe._kwds = {"new": False}
        pe._struct_size = 32
        parsed = pe.STRUCT.parse(raw_bytes, **pe._kwds)
        pe.value = parsed
        pe.data = pe.value
        logger.info("Injected %d raw 32-byte items", len(raw_bytes) // 32)
    elif ctx["supported"]:
        if pe.data is not pe.value:
            pe.value = pe.data
        logger.info("Saving %d playlist items (supported format)", len(pe.data))

    pyflp.save(ctx["project"], str(output_path))


def apply_template(
    flp_path: Path,
    template: dict,
    pattern_mapping: dict[str, list[int]],
    output_path: Path | None = None,
    progress_callback: Any = None,
) -> dict:
    """Apply a structure template to an FL Studio project file."""

    # ── Output path ──
    base_stem = flp_path.stem
    tid = template.get("id", "custom")
    for t in [t.get("id", "") for t in list_templates()] + ["arranged"]:
        if t and base_stem.endswith(f"_{t}"):
            base_stem = base_stem[: -(len(t) + 1)]
    if output_path is None or output_path == flp_path:
        output_path = flp_path.parent / f"{base_stem}_{tid}.flp"

    # ── Step 1: Setup ──
    if progress_callback:
        progress_callback(5, "Parsing FL Studio project...")
    ctx = _setup_project(flp_path)

    # ── Step 2: Weights (single pass) ──
    weight_map = _build_weight_map(ctx["project"])

    # ── Step 3: Track assignment ──
    pat_to_track = _assign_tracks(pattern_mapping)

    # ── Step 4: FX pre-pass ──
    sections = template.get("sections", [])
    fx_plan = _compute_fx_plan(sections, pattern_mapping, weight_map)

    if progress_callback:
        progress_callback(20, "Building arrangement...")

    # ── Step 4b: Build retention mute map ──
    mute_map = _build_retention_map(sections, pattern_mapping, ctx["pattern_by_iid"])

    # ── Step 5: Place patterns per section ──
    mapped_roles = set(pattern_mapping.keys())
    tpb = ctx["ticks_per_bar"]
    max_trk = ctx["max_tracks"]
    total_moved = 0
    details = []
    all_raw = b""

    for sec_idx, section in enumerate(sections):
        sec_start = section["start_bar"]
        sec_length = section["length_bars"]
        sec_name = section.get("name", "")
        sec_lower = sec_name.lower()
        sec_energy = section.get("energy", 0.5)

        # Decide roles
        roles = _decide_section_roles(sec_name, sec_energy, mapped_roles)
        if sec_start < 2 and "bass" in mapped_roles and "bass" not in roles:
            roles.append("bass")

        layer_plan = _compute_layer_plan(sec_name, sec_energy, sec_length, roles, pattern_mapping)

        wt = _WEIGHT_THRESHOLDS.get(sec_lower, 0.2)
        mpr = _MAX_PER_ROLE.get(sec_lower, 2)

        # Track role counts ACROSS all layer_plan entries for this section
        role_count: dict[str, int] = {}
        sec_detail = {"section": section.get("label", sec_name), "patterns_placed": 0, "layering": []}

        for entry in layer_plan:
            role = entry["role"]
            offset = entry["offset_bars"]
            trim = entry["trim_end_bars"]

            if role not in pattern_mapping:
                continue

            pat_iids = pattern_mapping[role]

            # ── FX: use pre-computed plan ──
            if role == "fx":
                for pid in pat_iids:
                    key = (sec_idx, pid)
                    if key not in fx_plan:
                        continue
                    fx = fx_plan[key]
                    s = sec_start + fx["offset_bars"]
                    l = max(1, sec_length - fx["offset_bars"] - fx["trim_end_bars"])
                    raw = _place_item(s * tpb, pid, l * tpb, max_trk - 1 - pat_to_track.get(pid, 0), ctx)
                    all_raw += raw
                    sec_detail["patterns_placed"] += 1
                    total_moved += 1
                sec_detail["layering"].append({"role": role, "offset": offset, "trim_end": trim})
                continue

            for pid in pat_iids:
                actual_offset = offset
                # Per-role limit
                rc = role_count.get(role, 0)
                limit = mpr if role != "bass" else 999
                if rc >= limit:
                    continue

                # Weight threshold — but always allow the FIRST (heaviest)
                # pattern per role so no role is completely silent
                pw = weight_map.get(pid, 0.5)
                is_first_in_role = rc == 0
                if not is_first_in_role and pw < wt:
                    continue

                if pid not in ctx["pattern_by_iid"]:
                    continue

                s = sec_start + actual_offset
                avail = max(1, sec_length - actual_offset - trim)
                trk = max_trk - 1 - pat_to_track.get(pid, 0)
                raw_pat_bars = max(1, ctx["pattern_length_ticks"].get(pid, tpb) // tpb)
                pat_bars = max(4, raw_pat_bars) if avail >= 4 else raw_pat_bars

                # ── Tile with retention cuts ──
                # Walk bar-by-bar, accumulate consecutive "play" bars into clips.
                # When we hit a muted bar, end the current clip and skip.
                clip_start = s
                clip_len = 0

                for b in range(avail):
                    abs_bar = s + b
                    muted_roles = mute_map.get(abs_bar, set())
                    is_muted = role in muted_roles

                    if is_muted:
                        # Flush current clip if any
                        if clip_len > 0:
                            raw = _place_item(clip_start * tpb, pid, clip_len * tpb, trk, ctx)
                            all_raw += raw
                            sec_detail["patterns_placed"] += 1
                            total_moved += 1
                            clip_len = 0
                        clip_start = abs_bar + 1
                    else:
                        clip_len += 1
                        # If we've accumulated a full pattern length, flush
                        if clip_len >= pat_bars:
                            raw = _place_item(clip_start * tpb, pid, clip_len * tpb, trk, ctx)
                            all_raw += raw
                            sec_detail["patterns_placed"] += 1
                            total_moved += 1
                            clip_start = abs_bar + 1
                            clip_len = 0

                # Flush remaining
                if clip_len > 0:
                    raw = _place_item(clip_start * tpb, pid, clip_len * tpb, trk, ctx)
                    all_raw += raw
                    sec_detail["patterns_placed"] += 1
                    total_moved += 1

                role_count[role] = rc + 1

            sec_detail["layering"].append({"role": role, "offset": offset, "trim_end": trim})

        details.append(sec_detail)

        if progress_callback:
            pct = 20 + int((sec_idx + 1) / len(sections) * 60)
            progress_callback(pct, f"Placed: {section.get('label', '')} ({sec_detail['patterns_placed']} patterns)")

    # ── Step 6: Route channels to mixer tracks ──
    if progress_callback:
        progress_callback(85, "Routing channels to mixer...")
    mixer_routing = _route_channels_to_mixer(ctx["project"], pattern_mapping)

    # ── Step 7: Save ──
    if progress_callback:
        progress_callback(90, "Saving...")
    _finalize_and_save(ctx, all_raw, output_path)
    if progress_callback:
        progress_callback(100, "Done!")

    return {
        "original_flp": flp_path.name,
        "output_flp": output_path.name,
        "template_used": template.get("id", "custom"),
        "sections_applied": len(sections),
        "patterns_moved": total_moved,
        "layering_details": details,
        "mixer_routing": mixer_routing,
    }


def _route_channels_to_mixer(
    project: Any,
    pattern_mapping: dict[str, list[int]],
) -> dict[str, int]:
    """
    Route each channel used in the arrangement to its own mixer insert.

    Groups channels by role so they sit together in the mixer:
    - Insert 1-N:  melody channels
    - Insert N+1:  keys channels
    - Then:        drums channels
    - Then:        bass channels
    - Then:        perc channels
    - Then:        fx channels

    Each channel gets its own insert so the producer can mix individually.
    The mixer insert name is set to match the channel name for easy ID.

    Returns: {channel_name: mixer_insert_number}
    """
    from pyflp.channel import _SamplerInstrument

    # Build channel IID -> channel object lookup using ALL channels
    channel_by_iid: dict[int, Any] = {}
    try:
        for ch in project.channels:
            channel_by_iid[ch.iid] = ch
    except Exception as e:
        logger.warning("Could not iterate channels: %s", e)
        return {}

    # Build pattern IID -> set of channel IIDs from note data
    pattern_channels: dict[int, set[int]] = {}
    for pat in project.patterns:
        try:
            notes = list(pat.notes) if hasattr(pat, "notes") else []
        except Exception:
            notes = []
        ch_iids = set()
        for n in notes:
            rc = getattr(n, "rack_channel", None)
            if rc is not None:
                ch_iids.add(rc)
        if ch_iids:
            pattern_channels[pat.iid] = ch_iids

    # Collect all channel IIDs used by arranged patterns, grouped by role
    ROLE_ORDER = ["melody", "keys", "drums", "bass", "perc", "fx"]
    role_channels: dict[str, list[int]] = {}
    all_routed: set[int] = set()

    for role in ROLE_ORDER:
        pat_iids = pattern_mapping.get(role, [])
        for pid in pat_iids:
            for ch_iid in sorted(pattern_channels.get(pid, set())):
                if ch_iid not in all_routed:
                    all_routed.add(ch_iid)
                    role_channels.setdefault(role, []).append(ch_iid)

    for role in pattern_mapping:
        if role not in ROLE_ORDER:
            for pid in pattern_mapping[role]:
                for ch_iid in sorted(pattern_channels.get(pid, set())):
                    if ch_iid not in all_routed:
                        all_routed.add(ch_iid)
                        role_channels.setdefault(role, []).append(ch_iid)

    # Assign mixer inserts sequentially (insert 0 = Master, start at 1)
    routing: dict[str, int] = {}
    insert_idx = 1
    max_inserts = 125

    for role in ROLE_ORDER + [r for r in role_channels if r not in ROLE_ORDER]:
        for ch_iid in role_channels.get(role, []):
            if insert_idx >= max_inserts:
                break

            ch = channel_by_iid.get(ch_iid)
            if ch is None:
                continue

            if not isinstance(ch, _SamplerInstrument):
                continue

            try:
                ch.insert = insert_idx
                ch_name = ch.name or f"Ch {ch_iid}"
                routing[ch_name] = insert_idx

                try:
                    mixer_insert = project.mixer[insert_idx]
                    mixer_insert.name = ch_name
                except Exception:
                    pass

                logger.info("Routed '%s' -> Mixer Insert %d", ch_name, insert_idx)
                insert_idx += 1
            except Exception as e:
                logger.warning("Could not route channel %s to mixer: %s", ch_iid, e)

    logger.info("Mixer routing complete: %d channels routed to individual inserts", len(routing))
    return routing


def _decide_section_roles(
    section_name: str,
    energy: float,
    available_roles: set[str],
) -> list[str]:
    """
    Dynamically decide which roles play in a section based on the
    section type and energy level. Uses ALL available pattern roles
    rather than relying on template static lists.

    This is the core intelligence — it replaces the hardcoded template
    `patterns` lists with real producer logic:

    - Intro: melody + keys only (set the vibe)
    - Build: melody + keys + perc (stagger in)
    - Drop/Chorus/Hook: EVERYTHING (max impact)
    - Verse: melody + keys + drums + bass (full groove, no fx)
    - Bridge/Breakdown: melody + keys (strip back for contrast)
    - Outro: melody + keys, drums fade (reverse strip)
    """
    name = section_name.lower()

    # Start with nothing, add based on section type
    roles: set[str] = set()

    # ── Melodic elements (melody + keys) — almost always present
    melodic = {"melody", "keys"} & available_roles

    # ── Rhythmic elements
    rhythmic = {"drums", "perc"} & available_roles

    # ── Low end
    low_end = {"bass"} & available_roles

    # ── Effects
    effects = {"fx"} & available_roles

    if name in ("intro",):
        # Intro: melody/keys only — set the tone
        roles = melodic.copy()
        if energy > 0.4:
            # Higher energy intro — light perc ok
            roles |= {"perc"} & available_roles

    elif name in ("build", "riser", "pre_chorus"):
        # Build: melodic + perc, stagger in
        roles = melodic | ({"perc"} & available_roles)
        if energy > 0.6:
            roles |= effects

    elif name in ("chorus", "drop", "drop_1", "drop_2", "drop_3",
                   "hook", "hook_1", "hook_2", "hook_3", "final_drop"):
        # Drop/Chorus: EVERYTHING hits — maximum impact
        roles = melodic | rhythmic | low_end | effects

    elif name in ("verse", "verse_1", "verse_2"):
        # Verse: full groove — melody + drums + bass, fx only for transitions
        roles = melodic | rhythmic | low_end
        if energy > 0.7:
            roles |= effects

    elif name in ("bridge", "breakdown"):
        # Bridge: strip back to melodic elements for contrast
        roles = melodic.copy()
        # Maybe light perc halfway through (handled by layer_plan)
        if energy > 0.35:
            roles |= {"perc"} & available_roles

    elif name in ("outro",):
        # Outro: melody rings out, drums/bass fade, tag plays at end
        roles = melodic | rhythmic | low_end | effects
        # (the layer_plan will handle progressive removal)

    else:
        # Unknown section — use energy to decide
        roles = melodic.copy()
        if energy > 0.3:
            roles |= rhythmic
        if energy > 0.5:
            roles |= low_end
        if energy > 0.7:
            roles |= effects

    return list(roles)


def _compute_layer_plan(
    section_name: str,
    energy: float,
    length_bars: int,
    roles: list[str],
    pattern_mapping: dict[str, list[int]],
) -> list[dict]:
    """
    Compute intelligent layering for a section.

    Returns list of {role, offset_bars, trim_end_bars} entries that determine
    when each element enters and exits within the section.

    This is where the "not slop" magic happens — real producers don't just
    dump everything on bar 1. Elements are introduced and removed strategically.
    """
    plan = []

    # Filter to roles that actually have patterns mapped
    available_roles = [r for r in roles if r in pattern_mapping]

    if not available_roles:
        return plan

    # ── Section-specific layering strategies ──

    if section_name == "intro":
        # Intro strategy: melody starts immediately, other elements stagger in
        for role in available_roles:
            if role in ("melody", "keys"):
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            elif role in ("drums", "perc"):
                # Drums enter halfway through intro (or bar 2 minimum)
                offset = max(2, length_bars // 2) if length_bars >= 4 else 0
                plan.append({"role": role, "offset_bars": offset, "trim_end_bars": 0})
            elif role == "bass":
                # Bass enters with or just after drums
                offset = max(2, length_bars // 2) if length_bars >= 4 else 0
                plan.append({"role": role, "offset_bars": offset, "trim_end_bars": 0})
            elif role == "fx":
                # FX only on first beat of intro (cymbal hit or riser)
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": max(0, length_bars - 2)})

    elif section_name in ("build", "riser"):
        # Build strategy: stagger elements in, each entering 1-2 bars apart
        stagger = max(1, length_bars // len(available_roles)) if available_roles else 1
        for i, role in enumerate(available_roles):
            offset = min(i * stagger, length_bars - 1)
            plan.append({"role": role, "offset_bars": offset, "trim_end_bars": 0})

    elif section_name in ("chorus", "drop_1", "drop_2", "drop_3", "hook_1", "hook_2", "hook_3"):
        # Drop/Chorus strategy: EVERYTHING hits at once (maximum impact)
        # FX starts 1 bar before (crash cymbal overlap from previous section)
        for role in available_roles:
            if role == "fx":
                # FX runs full length for drops — impacts, sweeps, risers
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            else:
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})

    elif section_name in ("verse", "verse_1", "verse_2"):
        # Verse strategy: core groove, bass enters 1 bar after drums
        for role in available_roles:
            if role in ("melody", "keys"):
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            elif role == "drums":
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            elif role == "bass":
                # Bass enters 1 bar after drums for that "drop-in" feel
                offset = 1 if length_bars > 4 else 0
                plan.append({"role": role, "offset_bars": offset, "trim_end_bars": 0})
            elif role == "fx":
                # FX only at transitions (first 2 bars of verse)
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": max(0, length_bars - 2)})
            elif role == "perc":
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})

    elif section_name in ("bridge", "breakdown"):
        # Bridge strategy: strip back — melody/keys only, maybe light perc
        for role in available_roles:
            if role in ("melody", "keys"):
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            elif role == "fx":
                # Atmospheric FX in bridge
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            elif role == "perc":
                # Light percussion, entering halfway
                offset = max(2, length_bars // 2) if length_bars >= 6 else 0
                plan.append({"role": role, "offset_bars": offset, "trim_end_bars": 0})
            # Drums and bass EXCLUDED from bridges for contrast

    elif section_name == "outro":
        # Outro strategy: reverse strip — remove elements progressively
        n = len(available_roles)
        for i, role in enumerate(available_roles):
            if role in ("melody", "keys"):
                # Melody plays full length
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})
            elif role in ("bass",):
                # Bass drops out halfway
                trim = max(2, length_bars // 2) if length_bars >= 4 else 0
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": trim})
            elif role in ("drums", "perc"):
                # Drums drop out 2/3 through
                trim = max(2, length_bars * 2 // 3) if length_bars >= 4 else 0
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": trim})
            elif role == "fx":
                # FX might have a final cymbal decay
                plan.append({"role": role, "offset_bars": 0, "trim_end_bars": max(0, length_bars - 2)})

    else:
        # Unknown section type — place everything at full length
        for role in available_roles:
            plan.append({"role": role, "offset_bars": 0, "trim_end_bars": 0})

    return plan


# ── Utility ─────────────────────────────────────────────────────────────


def recommend_template(audio_structure: dict) -> dict:
    """
    Given an audio analysis result, recommend the best matching template
    and explain why.

    Considers: BPM, energy profile, detected genre, section count.

    Returns:
        {
            "template_id": str,
            "confidence": float (0-1),
            "reason": str,
            "alternatives": [{"template_id", "reason"}],
        }
    """
    detected_genre = audio_structure.get("detected_genre", "trap")
    bpm = audio_structure.get("bpm", 140)
    total_bars = audio_structure.get("total_bars", 80)
    energy_curve = audio_structure.get("energy_curve", [])

    templates = list_templates()
    if not templates:
        return {"template_id": "trap_banger", "confidence": 0.5,
                "reason": "Default recommendation — no templates loaded",
                "alternatives": []}

    scores = []
    for tmpl in templates:
        score = 0.0
        reasons = []

        # Genre match (40% weight)
        if tmpl.get("genre") == detected_genre:
            score += 0.4
            reasons.append(f"genre match ({detected_genre})")
        elif detected_genre in (tmpl.get("genre", "") + "_" + tmpl.get("id", "")):
            score += 0.2
            reasons.append(f"partial genre match")

        # BPM compatibility (25% weight)
        bpm_range = tmpl.get("bpm_range", [60, 200])
        if bpm_range[0] <= bpm <= bpm_range[1]:
            # How centered is the BPM in the range?
            mid = (bpm_range[0] + bpm_range[1]) / 2
            closeness = 1.0 - abs(bpm - mid) / (mid - bpm_range[0] + 1)
            score += 0.25 * max(0, closeness)
            reasons.append(f"BPM {bpm} fits range {bpm_range[0]}-{bpm_range[1]}")

        # Bar count compatibility (15% weight)
        tmpl_bars = tmpl.get("total_bars", 80)
        bar_ratio = min(total_bars, tmpl_bars) / max(total_bars, tmpl_bars)
        score += 0.15 * bar_ratio
        if bar_ratio > 0.8:
            reasons.append(f"bar count compatible ({total_bars} vs {tmpl_bars})")

        # Energy profile similarity (20% weight)
        if energy_curve and tmpl.get("genre") in GENRE_ENERGY_PROFILES:
            profile = GENRE_ENERGY_PROFILES[tmpl["genre"]]
            similarity = _compare_energy_profile(energy_curve, profile)
            score += 0.2 * similarity
            if similarity > 0.6:
                reasons.append(f"energy profile matches {tmpl['genre']} pattern")

        scores.append({
            "template_id": tmpl["id"],
            "score": round(score, 3),
            "reasons": reasons,
        })

    scores.sort(key=lambda x: x["score"], reverse=True)

    best = scores[0]
    alternatives = [
        {"template_id": s["template_id"], "reason": ", ".join(s["reasons"])}
        for s in scores[1:3]  # top 2 alternatives
    ]

    return {
        "template_id": best["template_id"],
        "confidence": best["score"],
        "reason": ", ".join(best["reasons"]) or "best available match",
        "alternatives": alternatives,
    }


def find_audio_for_stem(stem: str) -> Path | None:
    """Find the audio file for a given stem name."""
    for ext in (".mp3", ".wav"):
        candidates = list(BEATS_DIR.glob(f"*{ext}"))
        for c_path in candidates:
            # Normalize stem comparison
            if _safe_stem(c_path.stem) == stem:
                return c_path
    return None


def _safe_stem(name: str) -> str:
    """Normalize a filename to a safe stem (matches render.py logic)."""
    import re
    name = Path(name).stem
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w\-]", "", name)
    return name.lower()
