import asyncio
import csv
import io
import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, Request,
    UploadFile, Form, Query
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_event_access, get_current_user_api, org_filter, require_admin, require_admin_api
from app.database import SessionLocal, get_db
from app.models import Attendee, Event, ImportJob, User
from app.schemas import AttendeeCreate, AttendeeOut, AttendeeUpdate, GHLContact
from app.services import ghl_service

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _attendee_to_dict(a: Attendee) -> dict:
    """Convert SQLAlchemy Attendee to a plain dict safe for JSON serialisation."""
    return {
        "id": a.id,
        "event_id": a.event_id,
        "ghl_contact_id": a.ghl_contact_id,
        "first_name": a.first_name,
        "last_name": a.last_name,
        "email": a.email,
        "phone": a.phone,
        "ticket_id": a.ticket_id,
        "qr_code_path": a.qr_code_path,
        "ticket_sent": a.ticket_sent,
        "ticket_sent_at": a.ticket_sent_at.isoformat() if a.ticket_sent_at else None,
        "checked_in": a.checked_in,
        "checked_in_at": a.checked_in_at.isoformat() if a.checked_in_at else None,
        "checked_out": bool(a.checked_out),
        "checked_out_at": a.checked_out_at.isoformat() if a.checked_out_at else None,
        "badge_issued": bool(a.badge_issued),
        "badge_issued_at": a.badge_issued_at.isoformat() if a.badge_issued_at else None,
        "company": a.company,
        "is_vip": bool(a.is_vip),
        "notes": a.notes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _is_vip_contact(tags: list) -> bool:
    """Return True if any GHL tag contains 'vip' (case-insensitive)."""
    return any("vip" in (t or "").lower() for t in tags)


# ── Page routes ──────────────────────────────────────────────

@router.get("/events/{event_id}/attendees", response_class=HTMLResponse)
async def attendees_list_page(
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
    return templates.TemplateResponse(
        "attendees/list.html",
        {"request": request, "event": event, "user": current_user},
    )


@router.get("/events/{event_id}/import", response_class=HTMLResponse)
async def import_page(
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
    attendees = db.query(Attendee).filter(Attendee.event_id == event_id).all()
    attendees_data = [_attendee_to_dict(a) for a in attendees]
    return templates.TemplateResponse(
        "attendees/import.html",
        {"request": request, "event": event, "attendees": attendees_data, "user": current_user},
    )


# ── JSON API routes ──────────────────────────────────────────

@router.get("/api/events/{event_id}/attendees", response_model=List[AttendeeOut])
async def list_attendees(
    event_id: int,
    skip: int = 0,
    limit: int = 1000,
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),  # all|registered|checked_in|badge_issued|checked_out
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied for this event.")
    query = db.query(Attendee).filter(Attendee.event_id == event_id)
    if search:
        s = f"%{search}%"
        query = query.filter(
            (Attendee.first_name.ilike(s))
            | (Attendee.last_name.ilike(s))
            | (Attendee.email.ilike(s))
            | (Attendee.company.ilike(s))
            | (Attendee.ticket_id.ilike(s))
        )
    if status == "registered":
        query = query.filter(Attendee.checked_in == False)
    elif status == "checked_in":
        query = query.filter(Attendee.checked_in == True, Attendee.checked_out == False)
    elif status == "badge_issued":
        query = query.filter(Attendee.badge_issued == True)
    elif status == "checked_out":
        query = query.filter(Attendee.checked_out == True)
    return query.order_by(Attendee.last_name, Attendee.first_name).offset(skip).limit(limit).all()


@router.post("/api/events/{event_id}/attendees", response_model=AttendeeOut)
async def add_attendee(
    event_id: int,
    payload: AttendeeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Prevent duplicate (same email in same event)
    if payload.email:
        existing = db.query(Attendee).filter(
            Attendee.event_id == event_id,
            Attendee.email == payload.email,
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Attendee with email {payload.email} already in event")

    attendee = Attendee(event_id=event_id, **payload.model_dump())
    db.add(attendee)
    db.commit()
    db.refresh(attendee)
    return attendee


@router.patch("/api/attendees/{attendee_id}", response_model=AttendeeOut)
async def update_attendee(
    attendee_id: int,
    payload: AttendeeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    # Fetch attendee and verify it belongs to the user's organisation
    attendee = (
        db.query(Attendee)
        .join(Event, Event.id == Attendee.event_id)
        .filter(Attendee.id == attendee_id)
        .filter(
            (Event.org_id == current_user.org_id) | (current_user.role == "developer")
        )
        .first()
    )
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(attendee, field, value)

    db.commit()
    db.refresh(attendee)
    return attendee


@router.delete("/api/attendees/{attendee_id}")
async def delete_attendee(
    attendee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    # Fetch attendee and verify it belongs to the user's organisation
    attendee = (
        db.query(Attendee)
        .join(Event, Event.id == Attendee.event_id)
        .filter(Attendee.id == attendee_id)
        .filter(
            (Event.org_id == current_user.org_id) | (current_user.role == "developer")
        )
        .first()
    )
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")
    db.delete(attendee)
    db.commit()
    return {"ok": True}


@router.post("/api/events/{event_id}/import-ghl")
async def import_from_ghl(
    event_id: int,
    tag: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """
    Creates a background import job and returns immediately.
    Poll /api/events/{id}/import-jobs/{job_id} for live progress.
    Handles up to 5,000 contacts via paginated GHL fetching.
    """
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not event.ghl_api_key or not event.ghl_location_id:
        raise HTTPException(status_code=400, detail="GHL API Key and Location ID must be set on this event.")

    job = ImportJob(event_id=event_id, status="pending", tag=tag or None)
    db.add(job)
    db.commit()
    db.refresh(job)

    event_data = {
        "id": event.id,
        "ghl_api_key": event.ghl_api_key,
        "ghl_location_id": event.ghl_location_id,
    }
    asyncio.create_task(_import_ghl_background(job.id, event_id, event_data, tag or None))

    return {"job_id": job.id}


@router.post("/api/events/{event_id}/import-ghl-selected")
async def import_selected_ghl_contacts(
    event_id: int,
    contacts: List[GHLContact],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Import a hand-picked list of GHL contacts (from the checkbox preview) directly."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    existing_emails = {
        e for (e,) in db.query(Attendee.email).filter(
            Attendee.event_id == event_id, Attendee.email.isnot(None)
        ).all()
    }
    existing_ghl_ids = {
        g for (g,) in db.query(Attendee.ghl_contact_id).filter(
            Attendee.event_id == event_id, Attendee.ghl_contact_id.isnot(None)
        ).all()
    }

    added = skipped = 0
    for c in contacts:
        email = (c.email or "").strip().lower()
        ghl_id = c.id

        if (email and email in existing_emails) or (ghl_id and ghl_id in existing_ghl_ids):
            skipped += 1
            continue

        db.add(Attendee(
            event_id=event_id,
            ghl_contact_id=ghl_id or None,
            first_name=c.first_name or None,
            last_name=c.last_name or None,
            email=email or None,
            phone=c.phone or None,
            company=c.company or None,
            is_vip=_is_vip_contact(c.tags),
        ))
        if email:
            existing_emails.add(email)
        if ghl_id:
            existing_ghl_ids.add(ghl_id)
        added += 1

    db.commit()
    return {"added": added, "skipped": skipped}


@router.get("/api/events/{event_id}/import-jobs/{job_id}")
async def get_import_job(
    event_id: int,
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Poll for live progress of a GHL import job."""
    job = db.query(ImportJob).filter(
        ImportJob.id == job_id,
        ImportJob.event_id == event_id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    errors = json.loads(job.errors_json) if job.errors_json else []
    return {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "fetched": job.fetched,
        "added": job.added,
        "skipped": job.skipped,
        "errors": errors[-20:],
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


# ── Background import worker ──────────────────────────────────

async def _import_ghl_background(
    job_id: int,
    event_id: int,
    event_data: dict,
    tag: Optional[str],
) -> None:
    """
    Fetches contacts from GHL page by page (100 at a time) and imports them.
    Writes progress after every page so the UI can show live counts.
    Handles up to 5,000 contacts.
    """
    MAX_CONTACTS = 5000
    PAGE_SIZE = 100
    added = skipped = fetched = 0
    errors: list = []

    # Mark running
    db = SessionLocal()
    try:
        job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
        if job:
            job.status = "running"
            db.commit()
    finally:
        db.close()

    api_key = event_data["ghl_api_key"]
    location_id = event_data["ghl_location_id"]

    # Pre-load existing emails + ghl_ids to avoid per-contact DB queries
    db = SessionLocal()
    try:
        existing_emails = {
            e for (e,) in db.query(Attendee.email).filter(
                Attendee.event_id == event_id, Attendee.email.isnot(None)
            ).all()
        }
        existing_ghl_ids = {
            g for (g,) in db.query(Attendee.ghl_contact_id).filter(
                Attendee.event_id == event_id, Attendee.ghl_contact_id.isnot(None)
            ).all()
        }
    finally:
        db.close()

    search_after = None

    while fetched < MAX_CONTACTS:
        # Fetch one page from GHL
        try:
            if tag:
                body = {
                    "locationId": location_id,
                    "filters": [{"field": "tags", "operator": "contains", "value": tag}],
                    "pageLimit": PAGE_SIZE,
                }
                if search_after:
                    body["searchAfter"] = search_after

                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(
                        "https://services.leadconnectorhq.com/contacts/search",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Version": "2021-07-28",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        json=body,
                    )
                    r.raise_for_status()
                    page_data = r.json()
                    page_contacts = page_data.get("contacts", [])
                    next_cursor = page_contacts[-1].get("searchAfter") if page_contacts else None
            else:
                # No tag — use the standard list endpoint
                params = {"locationId": location_id, "limit": PAGE_SIZE}
                if search_after:
                    params["startAfterId"] = search_after

                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(
                        "https://services.leadconnectorhq.com/contacts/",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Version": "2021-07-28",
                            "Accept": "application/json",
                        },
                        params=params,
                    )
                    r.raise_for_status()
                    page_data = r.json()
                    page_contacts = page_data.get("contacts", [])
                    next_cursor = page_data.get("meta", {}).get("startAfterId")

        except Exception as e:
            errors.append(f"GHL fetch error: {e}")
            logger.error(f"ImportJob {job_id}: GHL fetch error: {e}")
            break

        if not page_contacts:
            break

        # Import this page's contacts into the DB
        page_added = page_skipped = 0
        db = SessionLocal()
        try:
            for c in page_contacts:
                email = (c.get("email") or "").strip().lower()
                ghl_id = c.get("id", "")

                if (email and email in existing_emails) or (ghl_id and ghl_id in existing_ghl_ids):
                    page_skipped += 1
                    continue

                try:
                    first = c.get("firstName") or c.get("first_name") or ""
                    last = c.get("lastName") or c.get("last_name") or ""
                    tags = c.get("tags", [])
                    company = c.get("companyName") or c.get("businessName") or c.get("company") or ""
                    db.add(Attendee(
                        event_id=event_id,
                        ghl_contact_id=ghl_id or None,
                        first_name=first or None,
                        last_name=last or None,
                        email=email or None,
                        phone=c.get("phone") or None,
                        company=company or None,
                        is_vip=_is_vip_contact(tags),
                    ))
                    if email:
                        existing_emails.add(email)
                    if ghl_id:
                        existing_ghl_ids.add(ghl_id)
                    page_added += 1
                except Exception as e:
                    errors.append(f"{email or ghl_id}: {e}")

            db.commit()
        finally:
            db.close()

        fetched += len(page_contacts)
        added += page_added
        skipped += page_skipped

        # Persist progress
        db = SessionLocal()
        try:
            job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
            if job:
                job.fetched = fetched
                job.added = added
                job.skipped = skipped
                job.total = fetched   # we don't know total upfront, show fetched
                job.errors_json = json.dumps(errors[-50:])
                db.commit()
        finally:
            db.close()

        # Stop if this was the last page
        if len(page_contacts) < PAGE_SIZE or not next_cursor:
            break
        search_after = next_cursor

    # Mark complete
    db = SessionLocal()
    try:
        job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
        if job:
            job.status = "done"
            job.fetched = fetched
            job.added = added
            job.skipped = skipped
            job.total = fetched
            job.errors_json = json.dumps(errors[-50:])
            job.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()

    logger.info(f"ImportJob {job_id}: done — fetched={fetched} added={added} skipped={skipped}")


@router.post("/api/events/{event_id}/import-csv")
async def import_from_csv(
    event_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    contents = await file.read()
    try:
        text = contents.decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        text = contents.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    # Normalise header names
    def norm(s: str) -> str:
        return s.strip().lower().replace(" ", "_")

    added = 0
    skipped = 0
    errors = []

    # Pre-fetch existing emails for this event into a set for O(1) duplicate checks
    existing_emails = {
        email for (email,) in
        db.query(Attendee.email).filter(Attendee.event_id == event_id).all()
        if email
    }

    BATCH_SIZE = 500
    batch = 0

    for row in reader:
        row = {norm(k): (v or "").strip() for k, v in row.items()}
        email = (row.get("email", "") or row.get("email_address", "")).lower()
        if not email:
            errors.append(f"Row {added + skipped + len(errors) + 1}: missing email — {dict(list(row.items())[:3])}")
            continue

        if email in existing_emails:
            skipped += 1
            continue

        first_name = row.get("first_name", "") or row.get("firstname", "") or row.get("name", "")
        last_name  = row.get("last_name", "") or row.get("lastname", "") or row.get("surname", "")
        phone      = row.get("phone", "") or row.get("phone_number", "") or row.get("mobile", "")
        company    = row.get("company", "") or row.get("company_name", "") or row.get("organisation", "") or row.get("organization", "")

        db.add(Attendee(
            event_id=event_id,
            ghl_contact_id=None,   # matched lazily when tickets are sent
            first_name=first_name or None,
            last_name=last_name or None,
            email=email,
            phone=phone or None,
            company=company or None,
        ))
        existing_emails.add(email)   # guard against duplicates within the same file
        added += 1
        batch += 1

        # Commit in batches to avoid one giant transaction
        if batch >= BATCH_SIZE:
            db.commit()
            batch = 0

    db.commit()   # flush the final partial batch
    return {
        "added": added,
        "skipped": skipped,
        "note": "GHL contact IDs will be matched automatically when you send tickets.",
        "errors": errors[:20],
    }
