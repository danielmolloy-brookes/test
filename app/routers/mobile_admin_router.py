"""
Admin-facing API endpoints for mobile-app features:
  - Toggle push notifications per event
  - Upload/replace the event space map image
"""

import os
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth import require_admin_api
from app.database import get_db
from app.models import Event, User

router = APIRouter(prefix="/api/admin", tags=["admin"])

ALLOWED_MAP_TYPES = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
MAP_DIR = "static/maps"


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

    os.makedirs(MAP_DIR, exist_ok=True)
    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    dest = os.path.join(MAP_DIR, f"event_{event_id}_map.{ext}")

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    event.map_image_path = dest
    db.commit()
    return {"event_id": event_id, "map_path": dest}


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
