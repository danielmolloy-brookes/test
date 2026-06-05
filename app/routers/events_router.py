from typing import List, Optional

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_event_access, get_current_user_api, org_filter, require_admin, require_admin_api
from app.database import get_db
from app.models import Event, Attendee, EventFolder, EventPermission, Organisation, User
from app.schemas import EventCreate, EventUpdate, EventOut

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _event_stats(event: Event, db: Session) -> dict:
    total = db.query(Attendee).filter(Attendee.event_id == event.id).count()
    checked_in = db.query(Attendee).filter(
        Attendee.event_id == event.id, Attendee.checked_in == True
    ).count()
    checked_out = db.query(Attendee).filter(
        Attendee.event_id == event.id, Attendee.checked_out == True
    ).count()
    badge_issued = db.query(Attendee).filter(
        Attendee.event_id == event.id, Attendee.badge_issued == True
    ).count()
    sent = db.query(Attendee).filter(
        Attendee.event_id == event.id, Attendee.ticket_sent == True
    ).count()
    return {
        "total_attendees": total,
        "checked_in_count": checked_in,
        "checked_out_count": checked_out,
        "badge_issued_count": badge_issued,
        "tickets_sent_count": sent,
    }


# ── Page routes ──────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user

    # Only live (non-archived) events on the dashboard, scoped to org
    live_events = (
        org_filter(db.query(Event), current_user, Event)
        .filter(Event.is_archived == False)
        .order_by(Event.date.desc())
        .all()
    )
    folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
    archived_count = org_filter(db.query(Event), current_user, Event).filter(Event.is_archived == True).count()

    # Build enriched event dicts
    events_data = []
    for e in live_events:
        stats = _event_stats(e, db)
        events_data.append({
            "id": e.id,
            "name": e.name,
            "date": e.date,
            "location": e.location,
            "is_archived": e.is_archived,
            "folder_id": e.folder_id,
            "folder_name": e.folder.name if e.folder else None,
            "folder_color": e.folder.color if e.folder else "#6366f1",
            **stats,
        })

    # Group by folder for the dashboard view
    folder_groups = {}
    ungrouped = []
    for ed in events_data:
        fid = ed.get("folder_id")
        if fid:
            if fid not in folder_groups:
                folder_groups[fid] = {
                    "folder_id": fid,
                    "folder_name": ed["folder_name"],
                    "folder_color": ed["folder_color"],
                    "events": [],
                }
            folder_groups[fid]["events"].append(ed)
        else:
            ungrouped.append(ed)

    sorted_groups = sorted(folder_groups.values(), key=lambda g: (g["folder_name"] or "").lower())

    # Pass a small slice of recently archived events for the dashboard restore strip
    recently_archived = (
        org_filter(db.query(Event), current_user, Event)
        .filter(Event.is_archived == True)
        .order_by(Event.archived_at.desc())
        .limit(5)
        .all()
    )
    recently_archived_data = [
        {
            "id": e.id,
            "name": e.name,
            "date": e.date,
            "archived_at": e.archived_at,
            "folder_name": e.folder.name if e.folder else None,
            "folder_color": e.folder.color if e.folder else None,
        }
        for e in recently_archived
    ]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "events": events_data,
            "folder_groups": sorted_groups,
            "ungrouped": ungrouped,
            "folders": folders,
            "archived_count": archived_count,
            "recently_archived": recently_archived_data,
            "user": current_user,
        },
    )


@router.get("/events/create", response_class=HTMLResponse)
async def create_event_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    orgs = db.query(Organisation).order_by(Organisation.name).all() if current_user.is_developer else []
    folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
    return templates.TemplateResponse(
        "events/create.html",
        {"request": request, "user": current_user, "orgs": orgs, "folders": folders},
    )


def _resolve_folder(
    folder_id: str,
    new_folder_name: str,
    new_folder_color: str,
    db: Session,
    user_id: int,
    org_id: Optional[int] = None,
) -> Optional[int]:
    """Return the folder ID to use, creating a new folder if requested."""
    if new_folder_name.strip():
        existing = db.query(EventFolder).filter(
            EventFolder.name == new_folder_name.strip(),
            EventFolder.org_id == org_id,
        ).first()
        if existing:
            return existing.id
        folder = EventFolder(
            name=new_folder_name.strip(),
            color=new_folder_color or "#6366f1",
            created_by=user_id,
            org_id=org_id,
        )
        db.add(folder)
        db.flush()
        return folder.id
    if folder_id and folder_id not in ("", "none"):
        try:
            return int(folder_id)
        except ValueError:
            pass
    return None


@router.post("/events/create", response_class=HTMLResponse)
async def create_event_submit(
    request: Request,
    name: str = Form(...),
    date: str = Form(...),
    location: str = Form(""),
    description: str = Form(""),
    ghl_api_key: str = Form(""),
    ghl_location_id: str = Form(""),
    ghl_workflow_id: str = Form(""),
    ghl_attended_tag: str = Form(""),
    ghl_registered_tag: str = Form(""),
    ghl_checkout_tag: str = Form(""),
    folder_id: str = Form(""),
    new_folder_name: str = Form(""),
    new_folder_color: str = Form("#6366f1"),
    org_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user

    # Determine org_id for this event
    if current_user.is_developer:
        try:
            target_org_id = int(org_id) if org_id else None
        except ValueError:
            target_org_id = None
        if not target_org_id:
            orgs = db.query(Organisation).order_by(Organisation.name).all()
            folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
            return templates.TemplateResponse(
                "events/create.html",
                {"request": request, "user": current_user, "orgs": orgs, "folders": folders, "error": "Please select an organisation for this event."},
                status_code=400,
            )
    else:
        target_org_id = current_user.org_id

    try:
        event_date = datetime.fromisoformat(date)
    except ValueError:
        orgs = db.query(Organisation).order_by(Organisation.name).all() if current_user.is_developer else []
        folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
        return templates.TemplateResponse(
            "events/create.html",
            {"request": request, "user": current_user, "orgs": orgs, "folders": folders, "error": "Invalid date format"},
            status_code=400,
        )

    resolved_folder_id = _resolve_folder(folder_id, new_folder_name, new_folder_color, db, current_user.id, target_org_id)

    event = Event(
        name=name,
        date=event_date,
        location=location or None,
        description=description or None,
        ghl_api_key=ghl_api_key or None,
        ghl_location_id=ghl_location_id or None,
        ghl_workflow_id=ghl_workflow_id or None,
        ghl_attended_tag=ghl_attended_tag or f"Attended - {name}",
        ghl_registered_tag=ghl_registered_tag or None,
        ghl_checkout_tag=ghl_checkout_tag or None,
        folder_id=resolved_folder_id,
        org_id=target_org_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return RedirectResponse(url=f"/events/{event.id}", status_code=303)


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(
    request: Request,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    stats = _event_stats(event, db)
    folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
    return templates.TemplateResponse(
        "events/detail.html",
        {
            "request": request,
            "event": event,
            "stats": stats,
            "folders": folders,
            "user": current_user,
        },
    )


@router.get("/events/{event_id}/edit", response_class=HTMLResponse)
async def edit_event_page(
    request: Request,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
    orgs = db.query(Organisation).order_by(Organisation.name).all() if current_user.is_developer else []
    return templates.TemplateResponse(
        "events/create.html",
        {"request": request, "event": event, "folders": folders, "orgs": orgs, "user": current_user},
    )


@router.post("/events/{event_id}/edit", response_class=HTMLResponse)
async def edit_event_submit(
    request: Request,
    event_id: int,
    name: str = Form(...),
    date: str = Form(...),
    location: str = Form(""),
    description: str = Form(""),
    ghl_api_key: str = Form(""),
    ghl_location_id: str = Form(""),
    ghl_workflow_id: str = Form(""),
    ghl_attended_tag: str = Form(""),
    ghl_registered_tag: str = Form(""),
    ghl_checkout_tag: str = Form(""),
    folder_id: str = Form(""),
    new_folder_name: str = Form(""),
    new_folder_color: str = Form("#6366f1"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    try:
        event_date = datetime.fromisoformat(date)
    except ValueError:
        folders = org_filter(db.query(EventFolder), current_user, EventFolder).order_by(EventFolder.name).all()
        return templates.TemplateResponse(
            "events/create.html",
            {"request": request, "event": event, "folders": folders, "user": current_user, "error": "Invalid date format"},
            status_code=400,
        )

    event.name = name
    event.date = event_date
    event.location = location or None
    event.description = description or None
    event.ghl_api_key = ghl_api_key or None
    event.ghl_location_id = ghl_location_id or None
    event.ghl_workflow_id = ghl_workflow_id or None
    event.ghl_attended_tag = ghl_attended_tag or None
    event.ghl_registered_tag = ghl_registered_tag or None
    event.ghl_checkout_tag = ghl_checkout_tag or None
    event.folder_id = _resolve_folder(folder_id, new_folder_name, new_folder_color, db, current_user.id, event.org_id)
    db.commit()
    return RedirectResponse(url=f"/events/{event_id}", status_code=303)


# ── JSON API routes ──────────────────────────────────────────

@router.get("/api/events", response_model=List[EventOut])
async def api_list_events(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    events = org_filter(db.query(Event), current_user, Event).order_by(Event.date.desc()).all()
    result = []
    for e in events:
        stats = _event_stats(e, db)
        out = EventOut.model_validate({**e.__dict__, **stats})
        result.append(out)
    return result


@router.post("/api/events", response_model=EventOut)
async def api_create_event(
    payload: EventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = Event(**payload.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    stats = _event_stats(event, db)
    return EventOut.model_validate({**event.__dict__, **stats})


@router.get("/api/events/{event_id}", response_model=EventOut)
async def api_get_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    stats = _event_stats(event, db)
    return EventOut.model_validate({**event.__dict__, **stats})


@router.put("/api/events/{event_id}", response_model=EventOut)
async def api_update_event(
    event_id: int,
    payload: EventUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(event, field, value)
    db.commit()
    db.refresh(event)
    stats = _event_stats(event, db)
    return EventOut.model_validate({**event.__dict__, **stats})


@router.delete("/api/events/{event_id}")
async def api_delete_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    db.delete(event)
    db.commit()
    return {"ok": True}


@router.get("/api/events/{event_id}/stats")
async def api_event_stats(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Stats endpoint — accessible to any user with event permission (used by scanner)."""
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied for this event.")
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    stats = _event_stats(event, db)
    total = stats["total_attendees"]
    return {
        "event_id": event_id,
        "event_name": event.name,
        **stats,
        "percentage": round(stats["checked_in_count"] / total * 100, 1) if total > 0 else 0,
        "checkout_percentage": round(stats["checked_out_count"] / total * 100, 1) if total > 0 else 0,
        "registered_count": total - stats["checked_in_count"],
    }


# ── Duplicate event ──────────────────────────────────────────

@router.post("/api/events/{event_id}/duplicate")
async def api_duplicate_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Duplicate an event: copies all settings and attendees with fresh ticket IDs and cleared check-in state."""
    src = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Event not found")

    new_event = Event(
        name=f"{src.name} (Copy)",
        date=src.date,
        location=src.location,
        description=src.description,
        ghl_api_key=src.ghl_api_key,
        ghl_location_id=src.ghl_location_id,
        ghl_workflow_id=src.ghl_workflow_id,
        ghl_attended_tag=src.ghl_attended_tag,
        ghl_registered_tag=src.ghl_registered_tag,
        ghl_checkout_tag=src.ghl_checkout_tag,
        folder_id=src.folder_id,
        org_id=src.org_id,
        is_archived=False,
    )
    db.add(new_event)
    db.flush()  # assign new_event.id without committing

    # Copy all attendees: fresh ticket IDs, cleared check-in / badge / ticket-sent state
    src_attendees = db.query(Attendee).filter(Attendee.event_id == event_id).all()
    for a in src_attendees:
        db.add(Attendee(
            event_id=new_event.id,
            ghl_contact_id=a.ghl_contact_id,
            first_name=a.first_name,
            last_name=a.last_name,
            email=a.email,
            phone=a.phone,
            company=a.company,
            is_vip=a.is_vip,
            notes=a.notes,
            ticket_id=str(uuid.uuid4()),
        ))

    db.commit()
    return {
        "new_event_id": new_event.id,
        "name": new_event.name,
        "attendee_count": len(src_attendees),
    }


# ── Permission management API (admin only) ───────────────────

@router.get("/api/events/{event_id}/checkin-staff")
async def get_checkin_staff(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """List all check-in staff users with their access status for this event."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    # Only show checkin staff from the same org as the event
    staff_query = db.query(User).filter(User.role == "checkin")
    if not current_user.is_developer:
        staff_query = staff_query.filter(User.org_id == current_user.org_id)
    staff = staff_query.order_by(User.display_name).all()
    permitted_ids = {
        p.user_id
        for p in db.query(EventPermission).filter(EventPermission.event_id == event_id).all()
    }
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name or u.username,
            "has_access": u.id in permitted_ids,
        }
        for u in staff
    ]


@router.post("/api/events/{event_id}/permissions/{user_id}")
async def grant_event_access(
    event_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Grant a check-in user access to this event."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.is_checkin:
        raise HTTPException(status_code=400, detail="User is not a check-in staff member")
    existing = (
        db.query(EventPermission)
        .filter(EventPermission.user_id == user_id, EventPermission.event_id == event_id)
        .first()
    )
    if not existing:
        db.add(EventPermission(
            user_id=user_id,
            event_id=event_id,
            granted_by=current_user.id,
        ))
        db.commit()
    return {"ok": True}


@router.delete("/api/events/{event_id}/permissions/{user_id}")
async def revoke_event_access(
    event_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Revoke a check-in user's access to this event."""
    db.query(EventPermission).filter(
        EventPermission.user_id == user_id,
        EventPermission.event_id == event_id,
    ).delete()
    db.commit()
    return {"ok": True}
