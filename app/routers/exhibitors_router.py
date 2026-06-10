"""
Exhibitors tab — per-event list of exhibiting companies.

GET    /events/{id}/exhibitors                       — page
GET    /api/events/{id}/exhibitors                   — JSON list with check-in status
POST   /api/events/{id}/exhibitors                   — add single exhibitor
POST   /api/events/{id}/exhibitors/csv               — bulk CSV upload
POST   /api/events/{id}/exhibitors/map               — upload exhibitor map file
POST   /api/events/{id}/exhibitors/import-from-attendees
GET    /api/events/{id}/exhibitors/report            — CSV report download
POST   /api/exhibitors/{id}/checkin                  — manual check-in
POST   /api/exhibitors/{id}/undo-checkin             — undo manual check-in
DELETE /api/exhibitors/{id}                          — remove exhibitor
PATCH  /api/exhibitors/{id}                          — update location_code / notes
"""
import csv
import io
import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_event_access, get_current_user_api, org_filter, require_admin, require_admin_api
from app.database import get_db
from app.models import Attendee, Event, Exhibitor, User

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MAP_DIR = "static/exhibitor_maps"
os.makedirs(MAP_DIR, exist_ok=True)

ALLOWED_MAP_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"}
ALLOWED_MAP_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}


def _exhibitor_status(exhibitor: Exhibitor, db: Session) -> dict:
    """Return the exhibitor dict with live check-in status derived from attendees or manual override."""
    reps = (
        db.query(Attendee)
        .filter(
            Attendee.event_id == exhibitor.event_id,
            Attendee.company.ilike(exhibitor.company_name),
        )
        .all()
    )
    checked_in_reps = [r for r in reps if r.checked_in]
    attendee_checked_in = len(checked_in_reps) > 0
    checked_in = attendee_checked_in or bool(exhibitor.manually_checked_in)

    if exhibitor.manually_checked_in and exhibitor.manually_checked_in_at:
        checked_in_at = exhibitor.manually_checked_in_at
    else:
        checked_in_at = max(
            (r.checked_in_at for r in checked_in_reps if r.checked_in_at),
            default=None,
        )

    return {
        "id":                    exhibitor.id,
        "event_id":              exhibitor.event_id,
        "company_name":          exhibitor.company_name,
        "location_code":         exhibitor.location_code,
        "notes":                 exhibitor.notes,
        "rep_count":             len(reps),
        "checked_in":            checked_in,
        "manually_checked_in":   bool(exhibitor.manually_checked_in),
        "checked_in_at":         checked_in_at.isoformat() if checked_in_at else None,
        "checked_in_reps": [
            {"id": r.id, "full_name": r.full_name, "email": r.email, "checked_in_at": r.checked_in_at.isoformat() if r.checked_in_at else None}
            for r in checked_in_reps
        ],
    }


# ── Page ─────────────────────────────────────────────────────

@router.get("/events/{event_id}/exhibitors", response_class=HTMLResponse)
async def exhibitors_page(
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
    if not event.exhibitors_enabled:
        raise HTTPException(status_code=404, detail="Exhibitors not enabled for this event")
    return templates.TemplateResponse(
        "exhibitors/exhibitors.html",
        {"request": request, "event": event, "user": current_user},
    )


# ── JSON API ─────────────────────────────────────────────────

@router.get("/api/events/{event_id}/exhibitors")
async def list_exhibitors(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")
    exhibitors = (
        db.query(Exhibitor)
        .filter(Exhibitor.event_id == event_id)
        .order_by(Exhibitor.company_name)
        .all()
    )
    return [_exhibitor_status(e, db) for e in exhibitors]


@router.post("/api/events/{event_id}/exhibitors")
async def add_exhibitor(
    event_id: int,
    company_name: str = Form(...),
    location_code: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    company_name = company_name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="Company name is required")
    # Prevent duplicates
    existing = db.query(Exhibitor).filter(
        Exhibitor.event_id == event_id,
        Exhibitor.company_name.ilike(company_name),
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"'{company_name}' is already on the exhibitor list")
    exhibitor = Exhibitor(
        event_id=event_id,
        company_name=company_name,
        location_code=location_code.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(exhibitor)
    db.commit()
    db.refresh(exhibitor)
    return _exhibitor_status(exhibitor, db)


@router.patch("/api/exhibitors/{exhibitor_id}")
async def update_exhibitor(
    exhibitor_id: int,
    location_code: str = Form(""),
    notes: str = Form(""),
    company_name: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    exhibitor = db.query(Exhibitor).filter(Exhibitor.id == exhibitor_id).first()
    if not exhibitor:
        raise HTTPException(status_code=404, detail="Exhibitor not found")
    # Verify org access
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == exhibitor.event_id).first()
    if not event:
        raise HTTPException(status_code=403, detail="Access denied.")
    if company_name.strip():
        exhibitor.company_name = company_name.strip()
    exhibitor.location_code = location_code.strip() or None
    exhibitor.notes = notes.strip() or None
    db.commit()
    db.refresh(exhibitor)
    return _exhibitor_status(exhibitor, db)


@router.delete("/api/exhibitors/{exhibitor_id}")
async def delete_exhibitor(
    exhibitor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    exhibitor = db.query(Exhibitor).filter(Exhibitor.id == exhibitor_id).first()
    if not exhibitor:
        raise HTTPException(status_code=404, detail="Exhibitor not found")
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == exhibitor.event_id).first()
    if not event:
        raise HTTPException(status_code=403, detail="Access denied.")
    db.delete(exhibitor)
    db.commit()
    return {"ok": True}


@router.post("/api/exhibitors/{exhibitor_id}/checkin")
async def manual_checkin_exhibitor(
    exhibitor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    exhibitor = db.query(Exhibitor).filter(Exhibitor.id == exhibitor_id).first()
    if not exhibitor:
        raise HTTPException(status_code=404, detail="Exhibitor not found")
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == exhibitor.event_id).first()
    if not event:
        raise HTTPException(status_code=403, detail="Access denied.")
    exhibitor.manually_checked_in = True
    exhibitor.manually_checked_in_at = datetime.utcnow()
    db.commit()
    db.refresh(exhibitor)
    return _exhibitor_status(exhibitor, db)


@router.post("/api/exhibitors/{exhibitor_id}/undo-checkin")
async def undo_checkin_exhibitor(
    exhibitor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    exhibitor = db.query(Exhibitor).filter(Exhibitor.id == exhibitor_id).first()
    if not exhibitor:
        raise HTTPException(status_code=404, detail="Exhibitor not found")
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == exhibitor.event_id).first()
    if not event:
        raise HTTPException(status_code=403, detail="Access denied.")
    exhibitor.manually_checked_in = False
    exhibitor.manually_checked_in_at = None
    db.commit()
    db.refresh(exhibitor)
    return _exhibitor_status(exhibitor, db)


@router.get("/api/events/{event_id}/exhibitors/report")
async def exhibitors_report(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Download a CSV report of all exhibitors and their check-in status."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    exhibitors = (
        db.query(Exhibitor)
        .filter(Exhibitor.event_id == event_id)
        .order_by(Exhibitor.company_name)
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Company", "Location / Stand", "Status",
        "Checked In At", "Method", "Reps Registered",
        "Rep Names", "Notes",
    ])

    for ex in exhibitors:
        status = _exhibitor_status(ex, db)
        checked_in_at = status["checked_in_at"] or ""
        method = ""
        if status["checked_in"]:
            method = "Manual" if status["manually_checked_in"] else "Scanner"
        rep_names = ", ".join(r["full_name"] for r in status["checked_in_reps"]) if status["checked_in_reps"] else ""
        writer.writerow([
            ex.company_name,
            ex.location_code or "",
            "Checked In" if status["checked_in"] else "Not Arrived",
            checked_in_at,
            method,
            status["rep_count"],
            rep_names,
            ex.notes or "",
        ])

    output.seek(0)
    filename = f"exhibitors_{event.name.replace(' ', '_')}_{event_id}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/events/{event_id}/attendee-companies")
async def list_attendee_companies(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Return all unique company names from attendees for this event, with a flag for which are already exhibitors."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    attendees = (
        db.query(Attendee.company)
        .filter(Attendee.event_id == event_id, Attendee.company != None, Attendee.company != "")
        .distinct()
        .all()
    )
    existing_exhibitors = {
        e.company_name.lower()
        for e in db.query(Exhibitor.company_name).filter(Exhibitor.event_id == event_id).all()
    }
    companies = sorted({a.company.strip() for a in attendees if a.company and a.company.strip()})
    return [
        {"name": c, "already_exhibitor": c.lower() in existing_exhibitors}
        for c in companies
    ]


@router.post("/api/events/{event_id}/exhibitors/import-from-attendees")
async def import_from_attendees(
    event_id: int,
    companies: list = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Add selected company names as exhibitors."""
    from fastapi import Body
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"added": 0, "skipped": 0}  # handled via the JSON body endpoint below


@router.post("/api/events/{event_id}/exhibitors/bulk-add")
async def bulk_add_exhibitors(
    event_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Add a list of company names as exhibitors in one call."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    companies = payload.get("companies", [])
    added = skipped = 0
    for company in companies:
        company = company.strip()
        if not company:
            continue
        existing = db.query(Exhibitor).filter(
            Exhibitor.event_id == event_id,
            Exhibitor.company_name.ilike(company),
        ).first()
        if existing:
            skipped += 1
            continue
        db.add(Exhibitor(event_id=event_id, company_name=company))
        added += 1

    db.commit()
    return {"added": added, "skipped": skipped}


@router.post("/api/events/{event_id}/exhibitors/csv")
async def import_exhibitors_csv(
    event_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    # Accept flexible column names
    added = skipped = 0
    errors = []

    for i, row in enumerate(reader, start=2):
        # Find company name — accept "company", "company_name", "Company Name", etc.
        company = (
            row.get("company_name")
            or row.get("company")
            or row.get("Company Name")
            or row.get("Company")
            or ""
        ).strip()
        if not company:
            errors.append(f"Row {i}: missing company name — skipped")
            skipped += 1
            continue

        location_code = (
            row.get("location_code")
            or row.get("location")
            or row.get("Location Code")
            or row.get("Location")
            or row.get("Stand")
            or row.get("stand")
            or ""
        ).strip() or None

        notes = (row.get("notes") or row.get("Notes") or "").strip() or None

        existing = db.query(Exhibitor).filter(
            Exhibitor.event_id == event_id,
            Exhibitor.company_name.ilike(company),
        ).first()
        if existing:
            skipped += 1
            continue

        db.add(Exhibitor(
            event_id=event_id,
            company_name=company,
            location_code=location_code,
            notes=notes,
        ))
        added += 1

    db.commit()
    return {"ok": True, "added": added, "skipped": skipped, "errors": errors}


@router.post("/api/events/{event_id}/exhibitors/map")
async def upload_exhibitor_map(
    event_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_MAP_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {', '.join(ALLOWED_MAP_EXTS)}")

    content = await file.read()
    if len(content) > 20 * 1024 * 1024:  # 20 MB limit
        raise HTTPException(status_code=400, detail="File too large (max 20 MB)")

    # Delete old map if present
    if event.exhibitor_map_path:
        old = os.path.join(MAP_DIR, event.exhibitor_map_path)
        if os.path.exists(old):
            os.remove(old)

    filename = f"{event_id}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(MAP_DIR, filename)
    with open(path, "wb") as f:
        f.write(content)

    event.exhibitor_map_path = filename
    db.commit()
    return {"ok": True, "map_url": f"/static/exhibitor_maps/{filename}"}


@router.delete("/api/events/{event_id}/exhibitors/map")
async def delete_exhibitor_map(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.exhibitor_map_path:
        old = os.path.join(MAP_DIR, event.exhibitor_map_path)
        if os.path.exists(old):
            os.remove(old)
        event.exhibitor_map_path = None
        db.commit()
    return {"ok": True}
