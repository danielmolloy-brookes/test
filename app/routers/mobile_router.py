"""
Mobile app API — attendee self-service endpoints.

Authentication: Bearer token (JWT, same SECRET_KEY as the web app).
The token's `sub` field is the attendee's email address.

All routes are prefixed /api/mobile by main.py.
"""

import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

import bcrypt as _bcrypt
import qrcode
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Attendee, AttendeeUser, Event

router = APIRouter(prefix="/api/mobile", tags=["mobile"])

ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30


# ── Pydantic schemas ──────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class SetPasswordRequest(BaseModel):
    email: str
    password: str


class DeviceTokenRequest(BaseModel):
    token: str


class BookingResponse(BaseModel):
    attendee_id: int
    event_id: int
    event_name: str
    event_date: str
    ticket_id: str
    qr_url: Optional[str]
    checked_in: bool
    mobile_booked: bool


# ── Auth helpers ──────────────────────────────────────────────

def _hash(pw: str) -> str:
    return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()


def _verify(pw: str, hashed: str) -> bool:
    return _bcrypt.checkpw(pw.encode(), hashed.encode())


def _make_token(email: str) -> str:
    exp = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": email, "exp": exp, "typ": "mobile"}, settings.SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("typ") != "mobile":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def _get_attendee_user(request: Request, db: Session = Depends(get_db)) -> AttendeeUser:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1]
    email = _decode_token(token)
    if not email:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    au = db.query(AttendeeUser).filter(AttendeeUser.email == email).first()
    if not au:
        raise HTTPException(status_code=401, detail="Account not found")
    return au


def _ensure_qr(attendee: Attendee) -> None:
    """Generate QR code PNG if it doesn't exist yet."""
    if attendee.qr_code_path and os.path.exists(attendee.qr_code_path):
        return
    os.makedirs(settings.QR_CODE_DIR, exist_ok=True)
    path = os.path.join(settings.QR_CODE_DIR, f"{attendee.ticket_id}.png")
    img = qrcode.make(f"{settings.BASE_URL}/{attendee.ticket_id}")
    img.save(path)
    attendee.qr_code_path = path


# ── Routes ────────────────────────────────────────────────────

@router.post("/auth/check-email")
def check_email(body: dict, db: Session = Depends(get_db)):
    """
    Return whether an account exists for this email and whether a password
    has been set. The PWA uses this to decide: show Set Password vs Login.
    """
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "email required")

    # Does any Attendee exist with this email?
    exists_as_attendee = db.query(Attendee).filter(Attendee.email == email).first() is not None
    if not exists_as_attendee:
        raise HTTPException(404, "No attendee record found for this email address")

    au = db.query(AttendeeUser).filter(AttendeeUser.email == email).first()
    password_set = bool(au and au.password_hash)
    return {"email": email, "password_set": password_set}


@router.post("/auth/set-password")
def set_password(body: SetPasswordRequest, db: Session = Depends(get_db)):
    """First-time password setup. Email must match an existing Attendee record."""
    email = body.email.strip().lower()
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    attendee = db.query(Attendee).filter(Attendee.email == email).first()
    if not attendee:
        raise HTTPException(404, "No attendee record found for this email")

    au = db.query(AttendeeUser).filter(AttendeeUser.email == email).first()
    if au and au.password_hash:
        raise HTTPException(409, "Password already set — use login instead")

    if not au:
        au = AttendeeUser(email=email)
        db.add(au)

    au.password_hash = _hash(body.password)
    au.last_login_at = datetime.utcnow()
    db.commit()

    token = _make_token(email)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    au = db.query(AttendeeUser).filter(AttendeeUser.email == email).first()
    if not au or not au.password_hash:
        raise HTTPException(401, "Invalid email or password")
    if not _verify(body.password, au.password_hash):
        raise HTTPException(401, "Invalid email or password")

    au.last_login_at = datetime.utcnow()
    db.commit()

    token = _make_token(email)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
def get_me(au: AttendeeUser = Depends(_get_attendee_user), db: Session = Depends(get_db)):
    """Return profile + latest QR code (across all events, most recent first)."""
    attendees = (
        db.query(Attendee)
        .filter(Attendee.email == au.email)
        .order_by(Attendee.created_at.desc())
        .all()
    )
    # Ensure QR codes exist for all records
    for a in attendees:
        _ensure_qr(a)
    db.commit()

    # Use the most recent attendee record for the profile card
    primary = attendees[0] if attendees else None
    return {
        "email": au.email,
        "first_name": primary.first_name if primary else None,
        "last_name": primary.last_name if primary else None,
        "full_name": primary.full_name if primary else au.email,
        "company": primary.company if primary else None,
        "is_vip": primary.is_vip if primary else False,
        "ticket_id": primary.ticket_id if primary else None,
        "qr_url": primary.qr_url if primary else None,
    }


@router.get("/events")
def list_events(au: AttendeeUser = Depends(_get_attendee_user), db: Session = Depends(get_db)):
    """Return upcoming (non-archived) events."""
    now = datetime.utcnow()
    events = (
        db.query(Event)
        .filter(Event.is_archived == False, Event.date >= now)
        .order_by(Event.date.asc())
        .all()
    )

    # Gather event IDs where this user is already booked
    booked_ids = {
        a.event_id
        for a in db.query(Attendee).filter(Attendee.email == au.email).all()
    }

    return [
        {
            "id": e.id,
            "name": e.name,
            "date": e.date.isoformat(),
            "location": e.location,
            "description": e.description,
            "push_notifications_enabled": e.push_notifications_enabled,
            "has_map": bool(e.map_image_path),
            "is_booked": e.id in booked_ids,
        }
        for e in events
    ]


@router.post("/events/{event_id}/book")
def book_event(
    event_id: int,
    au: AttendeeUser = Depends(_get_attendee_user),
    db: Session = Depends(get_db),
):
    """Book the authenticated attendee onto an event."""
    event = db.query(Event).filter(Event.id == event_id, Event.is_archived == False).first()
    if not event:
        raise HTTPException(404, "Event not found")

    # Idempotent — don't double-book
    existing = (
        db.query(Attendee)
        .filter(Attendee.event_id == event_id, Attendee.email == au.email)
        .first()
    )
    if existing:
        _ensure_qr(existing)
        db.commit()
        return {
            "attendee_id": existing.id,
            "event_id": event.id,
            "event_name": event.name,
            "event_date": event.date.isoformat(),
            "ticket_id": existing.ticket_id,
            "qr_url": existing.qr_url,
            "checked_in": existing.checked_in,
            "mobile_booked": existing.mobile_booked,
            "already_booked": True,
        }

    # Look up profile from another event booking if available
    profile = db.query(Attendee).filter(Attendee.email == au.email).first()

    ticket_id = str(uuid.uuid4())
    attendee = Attendee(
        event_id=event_id,
        email=au.email,
        first_name=profile.first_name if profile else None,
        last_name=profile.last_name if profile else None,
        phone=profile.phone if profile else None,
        company=profile.company if profile else None,
        ticket_id=ticket_id,
        mobile_booked=True,
    )
    db.add(attendee)
    db.flush()  # get ID before QR generation

    _ensure_qr(attendee)
    db.commit()
    db.refresh(attendee)

    return {
        "attendee_id": attendee.id,
        "event_id": event.id,
        "event_name": event.name,
        "event_date": event.date.isoformat(),
        "ticket_id": attendee.ticket_id,
        "qr_url": attendee.qr_url,
        "checked_in": attendee.checked_in,
        "mobile_booked": attendee.mobile_booked,
        "already_booked": False,
    }


@router.get("/bookings")
def get_bookings(au: AttendeeUser = Depends(_get_attendee_user), db: Session = Depends(get_db)):
    """Return all events this attendee is registered for."""
    attendees = (
        db.query(Attendee)
        .filter(Attendee.email == au.email)
        .order_by(Attendee.created_at.desc())
        .all()
    )
    for a in attendees:
        _ensure_qr(a)
    db.commit()

    result = []
    for a in attendees:
        event = db.query(Event).filter(Event.id == a.event_id).first()
        if not event:
            continue
        result.append({
            "attendee_id": a.id,
            "event_id": event.id,
            "event_name": event.name,
            "event_date": event.date.isoformat(),
            "location": event.location,
            "ticket_id": a.ticket_id,
            "qr_url": a.qr_url,
            "checked_in": a.checked_in,
            "checked_in_at": a.checked_in_at.isoformat() if a.checked_in_at else None,
            "mobile_booked": a.mobile_booked,
            "push_notifications_enabled": event.push_notifications_enabled,
            "has_map": bool(event.map_image_path),
        })
    return result


@router.post("/device-token")
def register_device_token(
    body: DeviceTokenRequest,
    au: AttendeeUser = Depends(_get_attendee_user),
    db: Session = Depends(get_db),
):
    au.device_token = body.token
    db.commit()
    return {"status": "ok"}


@router.get("/events/{event_id}/map")
def get_event_map(
    event_id: int,
    au: AttendeeUser = Depends(_get_attendee_user),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event or not event.map_image_path:
        raise HTTPException(404, "No map available for this event")
    if not os.path.exists(event.map_image_path):
        raise HTTPException(404, "Map file not found")
    return FileResponse(event.map_image_path)


@router.get("/events/{event_id}/qr/{ticket_id}")
def get_qr_image(
    event_id: int,
    ticket_id: str,
    au: AttendeeUser = Depends(_get_attendee_user),
    db: Session = Depends(get_db),
):
    """Serve QR code PNG for a booking owned by the authenticated user."""
    attendee = (
        db.query(Attendee)
        .filter(
            Attendee.ticket_id == ticket_id,
            Attendee.event_id == event_id,
            Attendee.email == au.email,
        )
        .first()
    )
    if not attendee:
        raise HTTPException(403, "Not authorised")
    _ensure_qr(attendee)
    db.commit()
    path = os.path.join(settings.QR_CODE_DIR, f"{ticket_id}.png")
    return FileResponse(path, media_type="image/png")
