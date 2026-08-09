"""
/api/arrangement — FL Studio beat arrangement endpoints.

Phase 1: Template stamping (upload .flp, analyze, apply template, download)
Phase 2: Retention optimizer (stub)
Phase 3: Auto-arrange (stub)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.backend.config import ARRANGEMENTS_DIR, ARRANGEMENT_TEMPLATES_DIR
from app.backend.deps import get_current_user, UserContext
from app.backend.services import arrangement_svc
from app.backend.ws import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/arrangement", tags=["arrangement"])


# ── Request/Response Models ─────────────────────────────────────────────


class ApplyTemplateRequest(BaseModel):
    flp_filename: str
    template_id: str
    pattern_mapping: dict[str, list[int]]  # role -> list of pattern IIDs


class CustomTemplate(BaseModel):
    id: str
    name: str
    genre: str
    description: str = ""
    total_bars: int = 80
    bpm_range: list[int] = [60, 200]
    sections: list[dict]
    youtube_notes: dict = {}


# ── Template Endpoints ──────────────────────────────────────────────────


@router.get("/templates")
async def list_templates(user: UserContext = Depends(get_current_user)):
    """List all available arrangement templates."""
    return {"templates": arrangement_svc.list_templates()}


@router.get("/templates/{template_id}")
async def get_template(template_id: str, user: UserContext = Depends(get_current_user)):
    """Get a single arrangement template."""
    tmpl = arrangement_svc.get_template(template_id)
    if not tmpl:
        raise HTTPException(404, f"Template '{template_id}' not found")
    return tmpl


@router.post("/templates")
async def create_template(
    template: CustomTemplate,
    user: UserContext = Depends(get_current_user),
):
    """Create or update a custom arrangement template."""
    path = arrangement_svc.save_template(template.model_dump())
    return {"id": template.id, "message": f"Template saved: {path.name}"}


# ── FLP Upload & Analysis ──────────────────────────────────────────────


@router.post("/upload-flp")
async def upload_flp(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
):
    """Upload an FL Studio .flp file and return its structural analysis."""
    if not file.filename or not file.filename.lower().endswith(".flp"):
        raise HTTPException(400, "Only .flp files are accepted")

    ARRANGEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARRANGEMENTS_DIR / file.filename
    content = await file.read()

    if len(content) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(413, "File too large (max 50MB)")

    dest.write_bytes(content)
    logger.info("FLP uploaded: %s (%d bytes)", file.filename, len(content))

    # Parse in executor (blocking I/O)
    loop = asyncio.get_event_loop()
    try:
        analysis = await loop.run_in_executor(None, arrangement_svc.parse_flp, dest)
    except Exception as e:
        logger.error("FLP parse failed: %s", e)
        raise HTTPException(422, f"Failed to parse .flp file: {e}")

    return {"filename": file.filename, "analysis": analysis}


@router.get("/analyze-flp/{filename}")
async def analyze_flp(
    filename: str,
    user: UserContext = Depends(get_current_user),
):
    """Re-analyze a previously uploaded .flp file."""
    path = ARRANGEMENTS_DIR / filename
    if not path.exists():
        raise HTTPException(404, f"FLP file '{filename}' not found")

    loop = asyncio.get_event_loop()
    try:
        analysis = await loop.run_in_executor(None, arrangement_svc.parse_flp, path)
    except Exception as e:
        raise HTTPException(422, f"Failed to parse .flp: {e}")

    return analysis


# ── Audio Structure Analysis ────────────────────────────────────────────


@router.post("/analyze-audio/{stem}")
async def analyze_audio_structure(
    stem: str,
    user: UserContext = Depends(get_current_user),
):
    """Detect sections from a beat's audio file using spectral/energy analysis."""
    audio_path = arrangement_svc.find_audio_for_stem(stem)
    if not audio_path:
        raise HTTPException(404, f"No audio file found for stem '{stem}'")

    loop = asyncio.get_event_loop()
    try:
        structure = await loop.run_in_executor(
            None, arrangement_svc.detect_audio_structure, audio_path, None
        )
    except Exception as e:
        logger.error("Audio analysis failed for %s: %s", stem, e)
        raise HTTPException(500, f"Audio analysis failed: {e}")

    return structure


# ── Template Application ────────────────────────────────────────────────


@router.post("/recommend-template")
async def recommend_template(
    stem: str,
    user: UserContext = Depends(get_current_user),
):
    """
    Auto-recommend the best template for a beat based on audio analysis.
    Returns template ID, confidence score, and reasoning.
    """
    audio_path = arrangement_svc.find_audio_for_stem(stem)
    if not audio_path:
        raise HTTPException(404, f"No audio file found for stem '{stem}'")

    loop = asyncio.get_event_loop()
    try:
        structure = await loop.run_in_executor(
            None, arrangement_svc.detect_audio_structure, audio_path, None
        )
        recommendation = arrangement_svc.recommend_template(structure)
    except Exception as e:
        logger.error("Recommendation failed for %s: %s", stem, e)
        raise HTTPException(500, f"Recommendation failed: {e}")

    return {
        "stem": stem,
        "audio_analysis": structure,
        "recommendation": recommendation,
    }


@router.post("/apply-template")
async def apply_template(
    req: ApplyTemplateRequest,
    user: UserContext = Depends(get_current_user),
):
    """
    Apply an arrangement template to an uploaded .flp file.
    Returns immediately; progress streamed via WebSocket.
    """
    flp_path = ARRANGEMENTS_DIR / req.flp_filename
    if not flp_path.exists():
        raise HTTPException(404, f"FLP file '{req.flp_filename}' not found")

    template = arrangement_svc.get_template(req.template_id)
    if not template:
        raise HTTPException(404, f"Template '{req.template_id}' not found")

    if not req.pattern_mapping:
        raise HTTPException(400, "pattern_mapping is required")

    # Output filename
    output_name = f"{flp_path.stem}_{req.template_id}.flp"
    output_path = ARRANGEMENTS_DIR / output_name

    # Run in background
    task_id = f"arrange_{flp_path.stem}_{req.template_id}"

    async def _run():
        loop = asyncio.get_event_loop()

        async def progress(pct: int, detail: str):
            await manager.broadcast({
                "type": "progress",
                "stem": flp_path.stem,
                "phase": "arrangement",
                "pct": pct,
                "detail": detail,
            })

        def sync_progress(pct: int, detail: str):
            asyncio.run_coroutine_threadsafe(progress(pct, detail), loop)

        try:
            result = await loop.run_in_executor(
                None,
                arrangement_svc.apply_template,
                flp_path,
                template,
                req.pattern_mapping,
                output_path,
                sync_progress,
            )
            await manager.broadcast({
                "type": "arrangement_complete",
                "stem": flp_path.stem,
                "result": result,
            })
        except Exception as e:
            logger.error("Arrangement failed: %s", e)
            await manager.broadcast({
                "type": "arrangement_failed",
                "stem": flp_path.stem,
                "error": str(e),
            })

    asyncio.ensure_future(_run())

    return {
        "taskId": task_id,
        "status": "started",
        "output_filename": output_name,
    }


# ── Download ────────────────────────────────────────────────────────────


@router.get("/download/{filename}")
async def download_arranged_flp(
    filename: str,
    user: UserContext = Depends(get_current_user),
):
    """Download a modified .flp file."""
    path = ARRANGEMENTS_DIR / filename
    if not path.exists():
        raise HTTPException(404, f"File '{filename}' not found")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── List uploaded FLPs ──────────────────────────────────────────────────


@router.get("/files")
async def list_flp_files(user: UserContext = Depends(get_current_user)):
    """List all uploaded and generated .flp files."""
    ARRANGEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(ARRANGEMENTS_DIR.glob("*.flp"), key=lambda p: p.stat().st_mtime, reverse=True):
        files.append({
            "filename": f.name,
            "size": f.stat().st_size,
            "modified": f.stat().st_mtime,
            "is_arranged": "_arranged" in f.name or any(
                f"_{t['id']}" in f.name for t in arrangement_svc.list_templates()
            ),
        })
    return {"files": files}


# ── Phase 2/3 Stubs ────────────────────────────────────────────────────


@router.post("/optimize-retention/{stem}")
async def optimize_retention(stem: str, user: UserContext = Depends(get_current_user)):
    """Phase 2: Analyze and suggest retention-optimal timings."""
    raise HTTPException(501, "Retention optimizer coming in Phase 2")


@router.post("/one-click")
async def one_click_arrange(
    file: UploadFile = File(...),
    template_id: str | None = None,
    user: UserContext = Depends(get_current_user),
):
    """
    One-click arranger: upload .flp → parse → auto-detect roles →
    recommend best template → apply arrangement → return download info.

    Pass template_id to force a specific template (used by dice/shuffle).
    """
    if not file.filename or not file.filename.lower().endswith(".flp"):
        raise HTTPException(400, "Only .flp files are accepted")

    ARRANGEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARRANGEMENTS_DIR / file.filename
    content = await file.read()

    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 50MB)")

    dest.write_bytes(content)
    logger.info("One-click arrange: %s (%d bytes)", file.filename, len(content))

    loop = asyncio.get_event_loop()

    # Step 1: Parse .flp (auto-detects pattern roles)
    try:
        analysis = await loop.run_in_executor(None, arrangement_svc.parse_flp, dest)
    except Exception as e:
        raise HTTPException(422, f"Failed to parse .flp: {e}")

    mapping = analysis.get("suggested_mapping", {})
    if not mapping:
        raise HTTPException(422, "No patterns with notes found in this .flp")

    # Step 2: Find the matching audio file for BPM/genre detection
    stem = arrangement_svc._safe_stem(dest.stem)
    audio_path = arrangement_svc.find_audio_for_stem(stem)

    # Step 3: Pick template (use forced template_id if provided)
    if not template_id:
        if audio_path:
            try:
                audio_structure = await loop.run_in_executor(
                    None, arrangement_svc.detect_audio_structure, audio_path, None
                )
                recommendation = arrangement_svc.recommend_template(audio_structure)
                template_id = recommendation["template_id"]
            except Exception:
                template_id = _pick_template_from_bpm(analysis.get("tempo", 140))
        else:
            template_id = _pick_template_from_bpm(analysis.get("tempo", 140))

    template = arrangement_svc.get_template(template_id)
    if not template:
        templates = arrangement_svc.list_templates()
        template = templates[0] if templates else None
    if not template:
        raise HTTPException(500, "No arrangement templates available")

    # Step 4: Apply template with auto-detected mapping
    base_stem = dest.stem
    for tid in [t.get("id", "") for t in arrangement_svc.list_templates()] + ["arranged"]:
        if tid and base_stem.endswith(f"_{tid}"):
            base_stem = base_stem[: -(len(tid) + 1)]

    output_name = f"{base_stem}_{template['id']}.flp"
    output_path = ARRANGEMENTS_DIR / output_name

    try:
        result = await loop.run_in_executor(
            None,
            arrangement_svc.apply_template,
            dest, template, mapping, output_path, None,
        )
    except Exception as e:
        logger.error("One-click arrange failed: %s", e)
        raise HTTPException(500, f"Arrangement failed: {e}")

    return {
        "status": "complete",
        "output_filename": result["output_flp"],
        "template_used": template["name"],
        "template_id": template["id"],
        "genre": template.get("genre", ""),
        "tempo": analysis.get("tempo", 0),
        "patterns_detected": len(analysis.get("patterns", [])),
        "patterns_with_notes": len([p for p in analysis.get("patterns", []) if p.get("has_notes")]),
        "roles_detected": {role: len(ids) for role, ids in mapping.items()},
        "sections_applied": result["sections_applied"],
        "patterns_moved": result["patterns_moved"],
        "mixer_routing": result.get("mixer_routing", {}),
        "had_existing_arrangement": analysis.get("has_existing_arrangement", False),
    }


def _pick_template_from_bpm(bpm: float) -> str:
    """Quick template pick when no audio analysis is available.
    Default to drop_first — YouTube meta is hook on bar 1."""
    if bpm < 115:
        return "rnb_groove"
    return "drop_first"
