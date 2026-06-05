import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import org_filter, require_admin, require_admin_api
from app.database import get_db
from app.models import Attendee, Event, ScanLog, User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _delta_mins(start, end) -> Optional[int]:
    """Return whole minutes between two datetimes, or None if either is missing."""
    if not start or not end:
        return None
    secs = (end - start).total_seconds()
    return max(0, int(secs / 60))


def _fmt_delta(mins: Optional[int]) -> str:
    """Human-readable duration: '23m', '1h 5m', or '—'."""
    if mins is None:
        return "—"
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m"


def _build_attendee_row(a: Attendee) -> dict:
    """Enrich an ORM attendee with computed delta fields for display and CSV."""
    badge_mins   = _delta_mins(a.checked_in_at, a.badge_issued_at)
    checkout_mins = _delta_mins(a.checked_in_at, a.checked_out_at)
    return {
        "id":               a.id,
        "full_name":        a.full_name,
        "first_name":       a.first_name or "",
        "last_name":        a.last_name or "",
        "email":            a.email or "",
        "phone":            a.phone or "",
        "company":          a.company or "",
        "is_vip":           bool(a.is_vip),
        "notes":            a.notes or "",
        "ghl_contact_id":   a.ghl_contact_id or "",
        "ticket_id":        a.ticket_id or "",
        "ticket_sent":      bool(a.ticket_sent),
        "ticket_sent_at":   a.ticket_sent_at.isoformat() if a.ticket_sent_at else "",
        "checked_in":       bool(a.checked_in),
        "checked_in_at":    a.checked_in_at.isoformat() if a.checked_in_at else "",
        "checked_in_fmt":   a.checked_in_at.strftime("%H:%M:%S") if a.checked_in_at else "—",
        "badge_issued":     bool(a.badge_issued),
        "badge_issued_at":  a.badge_issued_at.isoformat() if a.badge_issued_at else "",
        "badge_issued_fmt": a.badge_issued_at.strftime("%H:%M:%S") if a.badge_issued_at else "—",
        "checkin_to_badge_mins": badge_mins,
        "checkin_to_badge_fmt":  _fmt_delta(badge_mins),
        "checked_out":      bool(a.checked_out),
        "checked_out_at":   a.checked_out_at.isoformat() if a.checked_out_at else "",
        "checked_out_fmt":  a.checked_out_at.strftime("%H:%M:%S") if a.checked_out_at else "—",
        "checkin_to_checkout_mins": checkout_mins,
        "checkin_to_checkout_fmt":  _fmt_delta(checkout_mins),
    }


@router.get("/reports/{event_id}", response_class=HTMLResponse)
async def report_page(
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

    attendees_orm = db.query(Attendee).filter(Attendee.event_id == event_id).all()
    attendees = [_build_attendee_row(a) for a in attendees_orm]

    total         = len(attendees)
    checked_in    = sum(1 for a in attendees if a["checked_in"])
    not_checked_in = total - checked_in
    badge_issued  = sum(1 for a in attendees if a["badge_issued"])
    checked_out   = sum(1 for a in attendees if a["checked_out"])
    tickets_sent  = sum(1 for a in attendees if a["ticket_sent"])
    percentage    = round(checked_in / total * 100, 1) if total > 0 else 0

    # Average time deltas (only for rows that have both timestamps)
    badge_deltas = [a["checkin_to_badge_mins"] for a in attendees if a["checkin_to_badge_mins"] is not None]
    checkout_deltas = [a["checkin_to_checkout_mins"] for a in attendees if a["checkin_to_checkout_mins"] is not None]
    avg_badge_mins    = round(sum(badge_deltas) / len(badge_deltas)) if badge_deltas else None
    avg_checkout_mins = round(sum(checkout_deltas) / len(checkout_deltas)) if checkout_deltas else None

    # Hourly check-in breakdown
    hourly: dict = {}
    for a in attendees:
        if a["checked_in"] and a["checked_in_at"]:
            hour = a["checked_in_at"][:13].split("T")[1][:2] + ":00"
            hourly[hour] = hourly.get(hour, 0) + 1
    hourly_labels = sorted(hourly.keys())
    hourly_counts = [hourly[h] for h in hourly_labels]

    return templates.TemplateResponse(
        "reports/attendance.html",
        {
            "request":          request,
            "event":            event,
            "attendees":        attendees,
            "total":            total,
            "checked_in":       checked_in,
            "not_checked_in":   not_checked_in,
            "badge_issued":     badge_issued,
            "checked_out":      checked_out,
            "tickets_sent":     tickets_sent,
            "percentage":       percentage,
            "avg_badge_mins":   avg_badge_mins,
            "avg_badge_fmt":    _fmt_delta(avg_badge_mins),
            "avg_checkout_mins": avg_checkout_mins,
            "avg_checkout_fmt": _fmt_delta(avg_checkout_mins),
            "hourly_labels":    hourly_labels,
            "hourly_counts":    hourly_counts,
            "user":             current_user,
        },
    )


@router.get("/api/events/{event_id}/export-csv")
async def export_csv(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    """Export full attendance report as CSV, including all timestamps and time deltas."""
    event = org_filter(db.query(Event), current_user, Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    attendees_orm = db.query(Attendee).filter(Attendee.event_id == event_id).order_by(
        Attendee.last_name, Attendee.first_name
    ).all()

    fieldnames = [
        "ticket_id",
        "first_name", "last_name", "email", "phone", "company",
        "is_vip",
        "ghl_contact_id",
        "ticket_sent", "ticket_sent_at",
        "checked_in", "checked_in_at",
        "badge_issued", "badge_issued_at", "mins_checkin_to_badge",
        "checked_out", "checked_out_at", "mins_checkin_to_checkout",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for a_orm in attendees_orm:
        a = _build_attendee_row(a_orm)
        writer.writerow({
            "ticket_id":      a["ticket_id"],
            "first_name":     a["first_name"],
            "last_name":      a["last_name"],
            "email":          a["email"],
            "phone":          a["phone"],
            "company":        a["company"],
            "is_vip":         "Yes" if a["is_vip"] else "No",
            "ghl_contact_id": a["ghl_contact_id"],
            "ticket_sent":    "Yes" if a["ticket_sent"] else "No",
            "ticket_sent_at": a["ticket_sent_at"],
            "checked_in":     "Yes" if a["checked_in"] else "No",
            "checked_in_at":  a["checked_in_at"],
            "badge_issued":   "Yes" if a["badge_issued"] else "No",
            "badge_issued_at": a["badge_issued_at"],
            "mins_checkin_to_badge": a["checkin_to_badge_mins"] if a["checkin_to_badge_mins"] is not None else "",
            "checked_out":    "Yes" if a["checked_out"] else "No",
            "checked_out_at": a["checked_out_at"],
            "mins_checkin_to_checkout": a["checkin_to_checkout_mins"] if a["checkin_to_checkout_mins"] is not None else "",
        })

    filename = f"{event.name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
