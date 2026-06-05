"""
Folder management and event archiving.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import org_filter, require_admin, require_admin_api
from app.database import get_db
from app.models import Attendee, Event, EventFolder, User
from app.schemas import FolderCreate, FolderUpdate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _event_card_data(e: Event, db: Session) -> dict:
    """Minimal dict for archive/folder event cards."""
    total = db.query(Attendee).filter(Attendee.event_id == e.id).count()
    checked = db.query(Attendee).filter(Attendee.event_id == e.id, Attendee.checked_in == True).count()
    return {
        "id": e.id,
        "name": e.name,
        "date": e.date,
        "location": e.location,
        "is_archived": e.is_archived,
        "archived_at": e.archived_at,
        "folder_id": e.folder_id,
        "folder_name": e.folder.name if e.folder else None,
        "folder_color": e.folder.color if e.folder else None,
        "total_attendees": total,
        "checked_in_count": checked,
    }


# ── Archive page ─────────────────────────────────────────────

@router.get("/archive", response_class=HTMLResponse)
async def archive_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user

    archived_events = (
        org_filter(db.query(Event), current_user, Event)
        .filter(Event.is_archived == True)
        .order_by(Event.archived_at.desc())
        .all()
    )
    folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()

    # Group by folder; key=None means "no folder"
    groups: dict = {}
    for e in archived_events:
        key = e.folder_id
        if key not in groups:
            groups[key] = {
                "folder_id": key,
                "folder_name": e.folder.name if e.folder else None,
                "folder_color": e.folder.color if e.folder else None,
                "events": [],
            }
        groups[key]["events"].append(_event_card_data(e, db))

    # Sort: named folders first (alpha), then uncategorised
    sorted_groups = sorted(
        groups.values(),
        key=lambda g: (g["folder_name"] is None, (g["folder_name"] or "").lower()),
    )

    return templates.TemplateResponse(
        "archive.html",
        {
            "request": request,
            "groups": sorted_groups,
            "folders": folders,
            "total": len(archived_events),
            "user": current_user,
        },
    )


# ── Folder CRUD API ──────────────────────────────────────────

@router.get("/api/folders")
async def list_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
    return [
        {
            "id": f.id,
            "name": f.name,
            "color": f.color or "#6366f1",
            "event_count": db.query(Event).filter(Event.folder_id == f.id).count(),
        }
        for f in folders
    ]


@router.post("/api/folders")
async def create_folder(
    payload: FolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    existing = db.query(EventFolder).filter(
        EventFolder.name == payload.name.strip(),
        EventFolder.org_id == current_user.org_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"A folder named '{payload.name}' already exists.")
    folder = EventFolder(
        name=payload.name.strip(),
        color=payload.color or "#6366f1",
        created_by=current_user.id,
        org_id=current_user.org_id,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return {"id": folder.id, "name": folder.name, "color": folder.color}


@router.put("/api/folders/{folder_id}")
async def update_folder(
    folder_id: int,
    payload: FolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    folder = db.query(EventFolder).filter(EventFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    if payload.name is not None:
        name = payload.name.strip()
        conflict = db.query(EventFolder).filter(
            EventFolder.name == name,
            EventFolder.id != folder_id,
            EventFolder.org_id == folder.org_id,
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail=f"A folder named '{name}' already exists.")
        folder.name = name
    if payload.color is not None:
        folder.color = payload.color
    db.commit()
    return {"ok": True, "id": folder.id, "name": folder.name, "color": folder.color}


@router.delete("/api/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    folder = db.query(EventFolder).filter(EventFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    # Detach events — do NOT delete them
    db.query(Event).filter(Event.folder_id == folder_id).update({"folder_id": None})
    db.delete(folder)
    db.commit()
    return {"ok": True}


# ── Event folder assignment API ───────────────────────────────

@router.patch("/api/events/{event_id}/folder")
async def set_event_folder(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    body = await request.json()
    folder_id = body.get("folder_id")   # int or null

    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if folder_id is not None:
        folder = db.query(EventFolder).filter(
            EventFolder.id == folder_id,
            EventFolder.org_id == event.org_id,
        ).first()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")

    event.folder_id = folder_id
    db.commit()
    return {"ok": True}


# ── Archive / unarchive API ───────────────────────────────────

@router.post("/api/events/{event_id}/archive")
async def archive_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.is_archived = True
    event.archived_at = datetime.utcnow()
    db.commit()
    logger.info(f"Archived event {event.id} '{event.name}'")
    return {"ok": True, "message": f"'{event.name}' has been archived."}


@router.post("/api/events/{event_id}/unarchive")
async def unarchive_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    event.is_archived = False
    event.archived_at = None
    db.commit()
    logger.info(f"Unarchived event {event.id} '{event.name}'")
    return {"ok": True, "message": f"'{event.name}' has been restored to the dashboard."}
