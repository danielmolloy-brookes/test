"""
Public slot-booking system.

Public (no auth):
  GET  /book/{event_id}                  — booking page
  GET  /api/book/{event_id}/slots        — available slots JSON
  POST /api/book/{event_id}/reserve      — make a booking
  GET  /book/confirmation/{ticket_id}    — confirmation page

Admin (cookie auth):
  PATCH /api/admin/events/{event_id}/booking          — toggle + configure
  POST  /api/admin/events/{event_id}/slots/generate   — generate time slots
  DELETE/api/admin/events/{event_id}/slots            — clear all slots
  GET   /api/admin/events/{event_id}/slot-bookings    — list bookings
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_admin_api
from app.database import get_db
from app.models import Event, EventSlot, Organisation, SlotBooking, User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Pydantic schemas ──────────────────────────────────────────

class BookingRequest(BaseModel):
    first_name: str
    last_name:  Optional[str] = None
    email:      str
    phone:      Optional[str] = None
    notes:      Optional[str] = None


class BookingConfig(BaseModel):
    enabled:            bool  = False
    slot_duration_mins: int   = 30
    slot_capacity:      int   = 1


class GenerateSlotsRequest(BaseModel):
    date:       str   # YYYY-MM-DD
    start_time: str   # HH:MM
    end_time:   str   # HH:MM


# ── Public routes ─────────────────────────────────────────────

@router.get("/org/{slug}", response_class=HTMLResponse)
def org_events_page(slug: str, request: Request, db: Session = Depends(get_db)):
    """Public listing of all upcoming events for an organisation."""
    org = db.query(Organisation).filter(Organisation.slug == slug).first()
    if not org:
        raise HTTPException(404, "Organisation not found")

    now = datetime.utcnow()
    upcoming = (
        db.query(Event)
        .filter(Event.org_id == org.id, Event.is_archived == False, Event.date >= now)
        .order_by(Event.date.asc())
        .all()
    )
    past = (
        db.query(Event)
        .filter(Event.org_id == org.id, Event.is_archived == False, Event.date < now)
        .order_by(Event.date.desc())
        .all()
    )
    events = upcoming + past

    # Attach a booking_url property-like value to each event
    # (templates can't call methods, so we build a simple list of dicts)
    enriched = []
    for ev in events:
        enriched.append({
            "id":                ev.id,
            "name":              ev.name,
            "date":              ev.date,
            "location":          ev.location,
            "description":       ev.description,
            "booking_enabled":   ev.booking_enabled,
            "slot_duration_mins": ev.slot_duration_mins,
            "booking_url":       f"/book/{ev.id}" if ev.booking_enabled else None,
            "is_past":           ev.date < now,
        })

    return templates.TemplateResponse(
        "booking/org_events.html",
        {"request": request, "org": org, "events": enriched, "now": now},
    )


@router.get("/book/{event_id}", response_class=HTMLResponse)
def booking_page(event_id: int, request: Request, db: Session = Depends(get_db)):
    event = db.query(Event).filter(
        Event.id == event_id, Event.booking_enabled == True, Event.is_archived == False
    ).first()
    if not event:
        raise HTTPException(404, "This event is not open for bookings")
    return templates.TemplateResponse(
        "booking/event.html",
        {"request": request, "event": event},
    )


@router.get("/api/book/{event_id}/slots")
def get_slots(event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(
        Event.id == event_id, Event.booking_enabled == True
    ).first()
    if not event:
        raise HTTPException(404, "Event not found or not bookable")

    now = datetime.utcnow()
    slots = (
        db.query(EventSlot)
        .filter(EventSlot.event_id == event_id, EventSlot.start_time > now)
        .order_by(EventSlot.start_time)
        .all()
    )
    return [
        {
            "id":           s.id,
            "start_time":   s.start_time.isoformat(),
            "end_time":     s.end_time.isoformat(),
            "capacity":     s.capacity,
            "booked_count": s.booked_count,
            "spots_left":   s.spots_left,
            "available":    s.is_available,
        }
        for s in slots
    ]


@router.post("/api/book/{event_id}/reserve")
def reserve_slot(
    event_id: int,
    slot_id: int,
    body: BookingRequest,
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(
        Event.id == event_id, Event.booking_enabled == True
    ).first()
    if not event:
        raise HTTPException(404, "Event not found or not bookable")

    # Lock and check the slot
    slot = db.query(EventSlot).filter(
        EventSlot.id == slot_id, EventSlot.event_id == event_id
    ).with_for_update().first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    if not slot.is_available:
        raise HTTPException(409, "This slot is fully booked")
    if slot.start_time < datetime.utcnow():
        raise HTTPException(410, "This slot has already passed")

    # Prevent double-booking same email for same slot
    existing = db.query(SlotBooking).filter(
        SlotBooking.slot_id == slot_id,
        SlotBooking.email == body.email.lower().strip(),
        SlotBooking.cancelled == False,
    ).first()
    if existing:
        raise HTTPException(409, "You already have a booking for this slot")

    booking = SlotBooking(
        slot_id    = slot_id,
        event_id   = event_id,
        first_name = body.first_name.strip(),
        last_name  = (body.last_name or "").strip() or None,
        email      = body.email.lower().strip(),
        phone      = (body.phone or "").strip() or None,
        notes      = (body.notes or "").strip() or None,
        ticket_id  = str(uuid.uuid4()),
    )
    slot.booked_count += 1
    db.add(booking)
    db.commit()
    db.refresh(booking)

    return {
        "ticket_id":  booking.ticket_id,
        "first_name": booking.first_name,
        "last_name":  booking.last_name,
        "email":      booking.email,
        "slot_start": slot.start_time.isoformat(),
        "slot_end":   slot.end_time.isoformat(),
        "event_name": event.name,
        "event_location": event.location,
    }


@router.get("/book/confirmation/{ticket_id}", response_class=HTMLResponse)
def booking_confirmation(ticket_id: str, request: Request, db: Session = Depends(get_db)):
    booking = db.query(SlotBooking).filter(SlotBooking.ticket_id == ticket_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    slot  = db.query(EventSlot).filter(EventSlot.id == booking.slot_id).first()
    event = db.query(Event).filter(Event.id == booking.event_id).first()
    return templates.TemplateResponse(
        "booking/confirmation.html",
        {"request": request, "booking": booking, "slot": slot, "event": event},
    )


# ── Admin API ─────────────────────────────────────────────────

@router.patch("/api/admin/events/{event_id}/booking")
def configure_booking(
    event_id: int,
    body: BookingConfig,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")

    event.booking_enabled    = body.enabled
    event.slot_duration_mins = max(5, body.slot_duration_mins)
    event.slot_capacity      = max(1, body.slot_capacity)
    db.commit()
    return {
        "booking_enabled":    event.booking_enabled,
        "slot_duration_mins": event.slot_duration_mins,
        "slot_capacity":      event.slot_capacity,
    }


@router.post("/api/admin/events/{event_id}/slots/generate")
def generate_slots(
    event_id: int,
    body: GenerateSlotsRequest,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")

    try:
        base_date  = datetime.strptime(body.date, "%Y-%m-%d").date()
        start_dt   = datetime.strptime(f"{body.date} {body.start_time}", "%Y-%m-%d %H:%M")
        end_dt     = datetime.strptime(f"{body.date} {body.end_time}",   "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(400, "Invalid date or time format (use YYYY-MM-DD and HH:MM)")

    if end_dt <= start_dt:
        raise HTTPException(400, "End time must be after start time")

    duration = timedelta(minutes=event.slot_duration_mins)
    created  = 0
    cursor   = start_dt
    while cursor + duration <= end_dt:
        slot = EventSlot(
            event_id     = event_id,
            start_time   = cursor,
            end_time     = cursor + duration,
            capacity     = event.slot_capacity,
            booked_count = 0,
        )
        db.add(slot)
        cursor  += duration
        created += 1

    db.commit()
    return {"created": created, "slot_duration_mins": event.slot_duration_mins}


@router.delete("/api/admin/events/{event_id}/slots")
def clear_slots(
    event_id: int,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    """Delete all future slots that have no bookings."""
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")

    future_empty = (
        db.query(EventSlot)
        .filter(EventSlot.event_id == event_id,
                EventSlot.start_time > datetime.utcnow(),
                EventSlot.booked_count == 0)
        .all()
    )
    count = len(future_empty)
    for s in future_empty:
        db.delete(s)
    db.commit()
    return {"deleted": count}


@router.get("/api/admin/events/{event_id}/slot-bookings")
def list_slot_bookings(
    event_id: int,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(404, "Event not found")
    if not current_user.is_developer and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")

    slots = (
        db.query(EventSlot)
        .filter(EventSlot.event_id == event_id)
        .order_by(EventSlot.start_time)
        .all()
    )
    result = []
    for s in slots:
        active = [b for b in s.bookings if not b.cancelled]
        result.append({
            "slot_id":      s.id,
            "start_time":   s.start_time.isoformat(),
            "end_time":     s.end_time.isoformat(),
            "capacity":     s.capacity,
            "booked_count": s.booked_count,
            "available":    s.is_available,
            "bookings": [
                {
                    "id":         b.id,
                    "full_name":  b.full_name,
                    "email":      b.email,
                    "phone":      b.phone,
                    "ticket_id":  b.ticket_id,
                    "cancelled":  b.cancelled,
                    "created_at": b.created_at.isoformat(),
                }
                for b in active
            ],
        })
    return result


@router.delete("/api/admin/slot-bookings/{booking_id}")
def cancel_booking(
    booking_id: int,
    current_user: User = Depends(require_admin_api),
    db: Session = Depends(get_db),
):
    booking = db.query(SlotBooking).filter(SlotBooking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    event = db.query(Event).filter(Event.id == booking.event_id).first()
    if not current_user.is_developer and event and event.org_id != current_user.org_id:
        raise HTTPException(403, "Not authorised")

    if not booking.cancelled:
        booking.cancelled = True
        slot = db.query(EventSlot).filter(EventSlot.id == booking.slot_id).first()
        if slot and slot.booked_count > 0:
            slot.booked_count -= 1
        db.commit()
    return {"status": "cancelled"}


# ── GDPR: data erasure request ────────────────────────────────

@router.post("/api/gdpr/erase")
def erase_personal_data(
    payload: dict,
    db: Session = Depends(get_db),
):
    """
    GDPR right-to-erasure endpoint.
    Anonymises all slot bookings matching the given email.
    Does not require auth — email acts as the credential.
    """
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email required")

    bookings = db.query(SlotBooking).filter(
        SlotBooking.email == email
    ).all()

    if not bookings:
        # Don't reveal whether email exists
        return {"status": "ok", "erased": 0}

    count = 0
    for b in bookings:
        b.first_name = "Deleted"
        b.last_name  = "User"
        b.email      = f"deleted_{b.id}@erased.invalid"
        b.phone      = None
        b.notes      = None
        b.cancelled  = True
        count += 1

    db.commit()
    return {"status": "ok", "erased": count}
