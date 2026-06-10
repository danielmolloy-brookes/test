import asyncio
import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import org_filter, require_admin, require_admin_api
from app.config import settings
from app.database import SessionLocal, get_db
from app.models import Attendee, Event, TicketJob, User
from app.schemas import SendTicketsRequest
from app.services import ghl_service, qr_service

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _attendee_to_dict(a: Attendee) -> dict:
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
    }


# ── Page routes ──────────────────────────────────────────────

@router.get("/events/{event_id}/tickets", response_class=HTMLResponse)
async def tickets_page(
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
    generated = sum(1 for a in attendees if a.qr_code_path)
    sent = sum(1 for a in attendees if a.ticket_sent)
    attendees_data = [_attendee_to_dict(a) for a in attendees]
    return templates.TemplateResponse(
        "tickets/generate.html",
        {
            "request": request,
            "event": event,
            "attendees": attendees_data,
            "generated": generated,
            "sent": sent,
            "user": current_user,
        },
    )


@router.get("/events/{event_id}/send-tickets", response_class=HTMLResponse)
async def send_tickets_page(
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
    return templates.TemplateResponse(
        "tickets/send.html",
        {"request": request, "event": event, "attendees": attendees, "user": current_user},
    )


# ── JSON API routes ──────────────────────────────────────────

@router.post("/api/events/{event_id}/generate-tickets")
async def generate_tickets(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Generate QR codes for all attendees in an event (skips those already generated)."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    attendees = db.query(Attendee).filter(Attendee.event_id == event_id).all()
    if not attendees:
        raise HTTPException(status_code=400, detail="No attendees found for this event")

    generated = 0
    skipped = 0
    errors = []

    for attendee in attendees:
        if attendee.qr_code_path:
            skipped += 1
            continue
        try:
            path = qr_service.generate_qr_code(
                ticket_id=attendee.ticket_id,
                attendee_name=attendee.full_name,
                event_name=event.name,
            )
            attendee.qr_code_path = path
            generated += 1
        except Exception as e:
            errors.append(f"Attendee {attendee.id}: {e}")
            logger.error(f"QR generation error for attendee {attendee.id}: {e}")

    db.commit()
    return {
        "generated": generated,
        "skipped": skipped,
        "total": len(attendees),
        "errors": errors[:10],
    }


@router.post("/api/events/{event_id}/regenerate-tickets")
async def regenerate_all_tickets(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Force-regenerate QR codes for ALL attendees (overwrites existing)."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    attendees = db.query(Attendee).filter(Attendee.event_id == event_id).all()
    generated = 0
    errors = []

    for attendee in attendees:
        try:
            path = qr_service.generate_qr_code(
                ticket_id=attendee.ticket_id,
                attendee_name=attendee.full_name,
                event_name=event.name,
            )
            attendee.qr_code_path = path
            generated += 1
        except Exception as e:
            errors.append(f"Attendee {attendee.id}: {e}")

    db.commit()
    return {"generated": generated, "total": len(attendees), "errors": errors[:10]}


@router.post("/api/events/{event_id}/send-tickets")
async def send_tickets(
    event_id: int,
    payload: SendTicketsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """
    Creates a background ticket-sending job and returns immediately.
    The actual sending runs asynchronously — poll /api/events/{id}/ticket-jobs/{job_id}
    for progress.
    """
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not event.ghl_workflow_id:
        raise HTTPException(
            status_code=400,
            detail="No GHL Workflow ID set for this event. Edit the event to add one.",
        )

    # Count attendees to be processed
    query = db.query(Attendee).filter(Attendee.event_id == event_id)
    if payload.attendee_ids:
        query = query.filter(Attendee.id.in_(payload.attendee_ids))
    elif not payload.resend_all:
        query = query.filter(Attendee.ticket_sent == False)

    attendee_ids = [a.id for a in query.all()]
    if not attendee_ids:
        return {"job_id": None, "total": 0, "message": "No attendees to send to"}

    # Create the job record
    job = TicketJob(
        event_id=event_id,
        status="pending",
        total=len(attendee_ids),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Snapshot the data the background task needs (don't pass ORM objects)
    job_id = job.id
    event_snapshot = {
        "id": event.id,
        "name": event.name,
        "date": event.date.strftime("%A, %d %B %Y %H:%M"),
        "location": event.location or "",
        "ghl_api_key": event.ghl_api_key or "",
        "ghl_location_id": event.ghl_location_id or "",
        "ghl_workflow_id": event.ghl_workflow_id or "",
    }

    # Fire-and-forget background task
    asyncio.create_task(
        _send_tickets_background(job_id, attendee_ids, event_snapshot)
    )

    return {"job_id": job_id, "total": len(attendee_ids)}


@router.get("/api/events/{event_id}/ticket-jobs/{job_id}")
async def get_ticket_job(
    event_id: int,
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Poll this endpoint for live progress of a ticket-sending job."""
    job = db.query(TicketJob).filter(
        TicketJob.id == job_id,
        TicketJob.event_id == event_id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    errors = json.loads(job.errors_json) if job.errors_json else []
    return {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "sent": job.sent,
        "failed": job.failed,
        "skipped": job.skipped,
        "errors": errors[-20:],   # last 20 errors
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


# ── Background worker ─────────────────────────────────────────

# Limit concurrent GHL calls to avoid hitting the GHL rate limit (429s).
# 4 concurrent means ~8 GHL requests in flight at once (2 per attendee: update + workflow).
# GHL rate-limits at ~100 req/min; 4 concurrent at ~500ms each = ~480 req/min peak,
# but the retry logic in ghl_service handles any 429s that slip through.
_GHL_SEMAPHORE = asyncio.Semaphore(4)


async def _send_one(
    attendee_id: int,
    event: dict,
) -> tuple[str, Optional[str]]:
    """
    Process a single attendee. Returns ("sent"|"failed"|"skipped", error_msg|None).
    Uses its own short-lived DB session — safe to call concurrently.
    """
    async with _GHL_SEMAPHORE:
        db = SessionLocal()
        try:
            attendee = db.query(Attendee).filter(Attendee.id == attendee_id).first()
            if not attendee:
                return ("failed", f"Attendee {attendee_id} not found")

            # Auto-generate QR if missing
            if not attendee.qr_code_path:
                try:
                    path = qr_service.generate_qr_code(
                        ticket_id=attendee.ticket_id,
                        attendee_name=attendee.full_name,
                        event_name=event["name"],
                    )
                    attendee.qr_code_path = path
                    db.commit()
                except Exception as e:
                    return ("failed", f"{attendee.email}: QR error — {e}")

            # Resolve GHL contact ID if missing
            if not attendee.ghl_contact_id and attendee.email and event["ghl_api_key"]:
                contact = await ghl_service.search_contacts_by_email(
                    api_key=event["ghl_api_key"],
                    location_id=event["ghl_location_id"],
                    email=attendee.email,
                )
                if contact:
                    attendee.ghl_contact_id = contact.get("id")
                    db.commit()

            if not attendee.ghl_contact_id:
                return ("skipped", f"{attendee.email}: no GHL contact ID")

            ticket_data = {
                "ticket_id": attendee.ticket_id,
                "qr_url": attendee.qr_url,
                "profile_url": f"{settings.BASE_URL}/{attendee.ticket_id}",
                "event_name": event["name"],
                "event_date": event["date"],
                "event_location": event["location"],
                "attendee_first_name": attendee.first_name or "",
                "attendee_last_name": attendee.last_name or "",
            }

            err = await ghl_service.send_ticket_via_workflow(
                api_key=event["ghl_api_key"],
                contact_id=attendee.ghl_contact_id,
                workflow_id=event["ghl_workflow_id"],
                ticket_data=ticket_data,
            )

            if err is None:
                attendee.ticket_sent = True
                attendee.ticket_sent_at = datetime.utcnow()
                db.commit()
                return ("sent", None)
            else:
                return ("failed", f"{attendee.email}: {err}")

        except Exception as e:
            logger.error(f"Unexpected error for attendee {attendee_id}: {e}")
            return ("failed", str(e))
        finally:
            db.close()


async def _send_tickets_background(
    job_id: int,
    attendee_ids: list,
    event: dict,
) -> None:
    """
    Background coroutine: processes all attendees concurrently (bounded by semaphore),
    writing progress to the TicketJob row after every batch.
    """
    db = SessionLocal()
    try:
        job = db.query(TicketJob).filter(TicketJob.id == job_id).first()
        if not job:
            return
        job.status = "running"
        db.commit()
    finally:
        db.close()

    sent = failed = skipped = 0
    errors: list = []

    BATCH = 20   # progress DB write every 20 attendees
    tasks = [_send_one(aid, event) for aid in attendee_ids]

    for i in range(0, len(tasks), BATCH):
        batch_results = await asyncio.gather(*tasks[i:i + BATCH], return_exceptions=True)

        for result in batch_results:
            if isinstance(result, Exception):
                failed += 1
                errors.append(str(result))
            else:
                outcome, err_msg = result
                if outcome == "sent":
                    sent += 1
                elif outcome == "skipped":
                    skipped += 1
                    if err_msg:
                        errors.append(err_msg)
                else:
                    failed += 1
                    if err_msg:
                        errors.append(err_msg)

        # Persist progress after each batch
        db = SessionLocal()
        try:
            job = db.query(TicketJob).filter(TicketJob.id == job_id).first()
            if job:
                job.sent = sent
                job.failed = failed
                job.skipped = skipped
                job.errors_json = json.dumps(errors[-50:])
                db.commit()
        finally:
            db.close()

    # Mark complete
    db = SessionLocal()
    try:
        job = db.query(TicketJob).filter(TicketJob.id == job_id).first()
        if job:
            job.status = "done"
            job.sent = sent
            job.failed = failed
            job.skipped = skipped
            job.errors_json = json.dumps(errors[-50:])
            job.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()

    logger.info(
        f"TicketJob {job_id}: done — sent={sent} failed={failed} skipped={skipped}"
    )


@router.get("/api/attendees/{attendee_id}/qr")
async def get_qr_image(
    attendee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Return QR code PNG for an attendee (generates if missing)."""
    attendee = db.query(Attendee).filter(Attendee.id == attendee_id).first()
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")

    event = db.query(Event).filter(Event.id == attendee.event_id).first()

    if not attendee.qr_code_path:
        path = qr_service.generate_qr_code(
            ticket_id=attendee.ticket_id,
            attendee_name=attendee.full_name,
            event_name=event.name if event else None,
        )
        attendee.qr_code_path = path
        db.commit()

    return FileResponse(attendee.qr_code_path, media_type="image/png")
