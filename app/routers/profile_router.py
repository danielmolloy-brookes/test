"""
Public attendee profile pages.

GET /{ticket_id}

No authentication required — the URL itself (containing the UUID ticket_id)
acts as a shareable credential.  Anyone who scans a QR code at the event can
view the profile card for that ticket.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Attendee, Event

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/{ticket_id}", response_class=HTMLResponse, include_in_schema=False)
def attendee_profile(ticket_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Public profile page for a scanned QR code.
    Matches on the ticket_id UUID embedded in each attendee's QR code.
    """
    attendee = (
        db.query(Attendee)
        .filter(Attendee.ticket_id == ticket_id)
        .first()
    )
    if not attendee:
        raise HTTPException(status_code=404, detail="Ticket not found")

    event = db.query(Event).filter(Event.id == attendee.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # If the event requires consent and attendee hasn't consented, show private page
    profile_private = event.profile_consent_enabled and not attendee.profile_consent

    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "attendee": attendee, "event": event, "profile_private": profile_private},
    )
