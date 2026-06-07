"""
Admin-facing API endpoints for mobile-app features:
  - Toggle push notifications per event
  - Upload/replace the event space map image
"""

import io
import os
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth import require_admin_api
from app.database import get_db
from app.models import Event, User

router = APIRouter(prefix="/api/admin", tags=["admin"])

ALLOWED_MAP_TYPES  = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
ALLOWED_IMG_TYPES  = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"}
MAP_DIR          = "static/maps"
BRAND_DIR        = "static/brand"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


async def _read_upload(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read upload into memory and enforce a size cap."""
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum allowed size is {max_bytes // (1024*1024)} MB.",
        )
    # Reset so subsequent shutil.copyfileobj works with a BytesIO
    file.file = io.BytesIO(data)
    return data


@router.patch("/events/{event_id}/push-notifications")
def toggle_push_notifications(
    event_id: int,
    body: dict,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    """Enable or disable push notifications for an event."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")

    enabled = bool(body.get("enabled", False))
    event.push_notifications_enabled = enabled
    db.commit()
    return {"event_id": event_id, "push_notifications_enabled": enabled}


@router.post("/events/{event_id}/map")
def upload_event_map(
    event_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    """Upload or replace the event space map image."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")
    if file.content_type not in ALLOWED_MAP_TYPES:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}")
    await _read_upload(file)

    os.makedirs(MAP_DIR, exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    dest = os.path.join(MAP_DIR, f"event_{event_id}_map.{ext}")

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    event.map_image_path = dest
    db.commit()
    return {"event_id": event_id, "map_path": dest}


@router.patch("/events/{event_id}/branding")
def update_branding_colours(
    event_id: int,
    body: dict,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    """Save primary / secondary / tertiary brand colours."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")
    for key in ("brand_primary", "brand_secondary", "brand_tertiary"):
        val = body.get(key)
        if val and isinstance(val, str) and val.startswith("#") and len(val) in (4, 7):
            setattr(event, key, val)
    db.commit()
    return {"status": "ok"}


@router.post("/events/{event_id}/brand-backdrop")
def upload_brand_backdrop(
    event_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")
    if file.content_type not in ALLOWED_IMG_TYPES:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}")
    await _read_upload(file)
    os.makedirs(BRAND_DIR, exist_ok=True)
    ext  = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    dest = os.path.join(BRAND_DIR, f"event_{event_id}_backdrop.{ext}")
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    event.brand_backdrop_path = dest
    db.commit()
    return {"path": dest, "url": f"/static/brand/event_{event_id}_backdrop.{ext}"}


@router.delete("/events/{event_id}/brand-backdrop")
def delete_brand_backdrop(
    event_id: int,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if event.brand_backdrop_path and os.path.exists(event.brand_backdrop_path):
        os.remove(event.brand_backdrop_path)
    event.brand_backdrop_path = None
    db.commit()
    return {"status": "deleted"}


@router.post("/events/{event_id}/brand-logo")
def upload_brand_logo(
    event_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")
    if file.content_type not in ALLOWED_IMG_TYPES:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}")
    await _read_upload(file)
    os.makedirs(BRAND_DIR, exist_ok=True)
    ext  = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    dest = os.path.join(BRAND_DIR, f"event_{event_id}_logo.{ext}")
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    event.brand_logo_path = dest
    db.commit()
    return {"path": dest, "url": f"/static/brand/event_{event_id}_logo.{ext}"}


@router.delete("/events/{event_id}/brand-logo")
def delete_brand_logo(
    event_id: int,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if event.brand_logo_path and os.path.exists(event.brand_logo_path):
        os.remove(event.brand_logo_path)
    event.brand_logo_path = None
    db.commit()
    return {"status": "deleted"}


@router.delete("/events/{event_id}/map")
def delete_event_map(
    event_id: int,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if event.map_image_path and os.path.exists(event.map_image_path):
        os.remove(event.map_image_path)
    event.map_image_path = None
    db.commit()
    return {"status": "deleted"}
