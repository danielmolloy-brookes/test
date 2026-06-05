import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import check_event_access, get_current_user_api, require_login
from app.database import get_db
from app.models import Attendee, Event, ScanLog, User
from app.realtime import broadcaster
from app.schemas import ManualCheckIn, ScanRequest, ScanResult
from app.services import ghl_service

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Realtime helpers ─────────────────────────────────────────

def _get_stats(event_id: int, db: Session) -> dict:
    """Return current counts for an event."""
    total      = db.query(Attendee).filter(Attendee.event_id == event_id).count()
    checked_in = db.query(Attendee).filter(Attendee.event_id == event_id, Attendee.checked_in  == True).count()
    checked_out= db.query(Attendee).filter(Attendee.event_id == event_id, Attendee.checked_out == True).count()
    badge_issued=db.query(Attendee).filter(Attendee.event_id == event_id, Attendee.badge_issued == True).count()
    return {"total": total, "checked_in": checked_in, "checked_out": checked_out, "badge_issued": badge_issued}


def _fmt_log(log: ScanLog, attendee: Optional[Attendee]) -> dict:
    """Serialise a scan log entry the same way the activity API does."""
    return {
        "id":               log.id,
        "attendee_id":      attendee.id        if attendee else None,
        "scanned_at":       log.scanned_at.isoformat() if log.scanned_at else None,
        "result":           log.result,
        "full_name":        attendee.full_name  if attendee else None,
        "email":            attendee.email      if attendee else None,
        "company":          attendee.company    if attendee else None,
        "is_vip":           bool(attendee.is_vip)      if attendee else False,
        "badge_issued":     bool(attendee.badge_issued) if attendee else False,
        "profile_consent":  bool(attendee.profile_consent) if attendee else False,
        "notes":            attendee.notes      if attendee else None,
        "scanned_ticket_id":log.scanned_ticket_id,
    }


# ── Page routes ──────────────────────────────────────────────

@router.get("/checkin/{event_id}", response_class=HTMLResponse)
async def checkin_page(
    request: Request,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_login),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not check_event_access(current_user, event_id, db):
        return templates.TemplateResponse(
            "checkin/no_access.html",
            {"request": request, "event": event, "user": current_user},
            status_code=403,
        )
    total = db.query(Attendee).filter(Attendee.event_id == event_id).count()
    checked_in = db.query(Attendee).filter(
        Attendee.event_id == event_id, Attendee.checked_in == True
    ).count()
    return templates.TemplateResponse(
        "checkin/scanner.html",
        {
            "request": request,
            "event": event,
            "total": total,
            "checked_in": checked_in,
            "user": current_user,
        },
    )


# ── Server-Sent Events stream ────────────────────────────────

@router.get("/api/events/{event_id}/live")
async def live_stream(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """
    SSE endpoint — clients subscribe here and receive pushed events
    whenever check-ins, check-outs, badge updates, etc. happen.
    The browser EventSource API reconnects automatically on drop.
    """
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")

    async def generator():
        q = await broadcaster.subscribe(event_id)
        try:
            # Initial ping so the client knows it's connected
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield msg
                except asyncio.TimeoutError:
                    # Keepalive comment — prevents proxy/load-balancer timeouts
                    yield ": ping\n\n"
        finally:
            broadcaster.unsubscribe(event_id, q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # tells nginx not to buffer SSE
            "Connection": "keep-alive",
        },
    )


# ── JSON API routes ──────────────────────────────────────────

@router.post("/api/checkin/scan", response_model=ScanResult)
async def scan_ticket(
    payload: ScanRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """
    Process a QR code scan.
    Returns success, duplicate, or not_found.
    """
    if not check_event_access(current_user, payload.event_id, db):
        raise HTTPException(status_code=403, detail="You do not have permission to check in at this event.")

    device_info = payload.device_info or request.headers.get("User-Agent", "")[:200]

    # Look up ticket — must belong to the correct event
    attendee = (
        db.query(Attendee)
        .filter(
            Attendee.ticket_id == payload.resolved_ticket_id,
            Attendee.event_id == payload.event_id,
        )
        .first()
    )

    if not attendee:
        # Could be wrong event or genuinely invalid — check if ticket exists at all
        wrong_event = (
            db.query(Attendee)
            .filter(Attendee.ticket_id == payload.resolved_ticket_id)
            .first()
        )
        if wrong_event:
            logger.warning(
                f"Scan: ticket {payload.resolved_ticket_id[:12]} belongs to event "
                f"{wrong_event.event_id}, not {payload.event_id}"
            )
            log = ScanLog(
                attendee_id=None,
                event_id=payload.event_id,
                scanned_ticket_id=payload.resolved_ticket_id[:36],
                device_info=device_info,
                result="wrong_event",
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            await broadcaster.publish(payload.event_id, {"type": "activity", "entry": _fmt_log(log, None)})
            return ScanResult(
                success=False,
                status="wrong_event",
                message="❌ This ticket is for a different event.",
            )
        logger.warning(f"Scan: ticket not found — {payload.resolved_ticket_id}")
        log = ScanLog(
            attendee_id=None,
            event_id=payload.event_id,
            scanned_ticket_id=payload.resolved_ticket_id[:36],
            device_info=device_info,
            result="not_found",
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        await broadcaster.publish(payload.event_id, {"type": "activity", "entry": _fmt_log(log, None)})
        return ScanResult(
            success=False,
            status="not_found",
            message="❌ Ticket not found. Please check the QR code.",
        )

    if attendee.checked_in:
        # Duplicate scan — log it
        log = ScanLog(attendee_id=attendee.id, device_info=device_info, result="duplicate")
        db.add(log)
        db.commit()
        db.refresh(log)
        logger.warning(f"Duplicate scan for attendee {attendee.id} ({attendee.email})")
        await broadcaster.publish(attendee.event_id, {"type": "activity", "entry": _fmt_log(log, attendee)})
        return ScanResult(
            success=False,
            status="duplicate",
            message=f"⚠️ {attendee.full_name} already checked in at {attendee.checked_in_at.strftime('%H:%M') if attendee.checked_in_at else 'earlier'}",
            attendee=attendee,
        )

    # ── Successful check-in ───────────────────────────────────
    attendee.checked_in = True
    attendee.checked_in_at = datetime.utcnow()

    log = ScanLog(attendee_id=attendee.id, device_info=device_info, result="success")
    db.add(log)
    db.commit()
    db.refresh(attendee)
    db.refresh(log)

    # Update GHL in background (fire-and-forget)
    event = db.query(Event).filter(Event.id == attendee.event_id).first()
    if attendee.ghl_contact_id and event:
        try:
            await ghl_service.process_checkin_in_ghl(
                api_key=event.ghl_api_key or "",
                contact_id=attendee.ghl_contact_id,
                attended_tag=event.ghl_attended_tag,
                registered_tag=event.ghl_registered_tag,
            )
        except Exception as e:
            logger.error(f"GHL check-in update failed for {attendee.id}: {e}")

    await broadcaster.publish(attendee.event_id, {
        "type": "activity",
        "entry": _fmt_log(log, attendee),
        "stats": _get_stats(attendee.event_id, db),
    })
    logger.info(f"✅ Checked in: {attendee.full_name} ({attendee.email}){' [VIP]' if attendee.is_vip else ''}")
    return ScanResult(
        success=True,
        status="checked_in",
        message=f"✅ Welcome, {attendee.first_name or attendee.full_name}!",
        attendee=attendee,
        is_vip=bool(attendee.is_vip),
    )


@router.post("/api/checkin/manual", response_model=ScanResult)
async def manual_checkin(
    payload: ManualCheckIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Manually check in an attendee by ID."""
    attendee = db.query(Attendee).filter(Attendee.id == payload.attendee_id).first()
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")
    if not check_event_access(current_user, attendee.event_id, db):
        raise HTTPException(status_code=403, detail="You do not have permission to check in at this event.")

    if attendee.checked_in:
        return ScanResult(
            success=False,
            status="duplicate",
            message=f"{attendee.full_name} is already checked in",
            attendee=attendee,
        )

    attendee.checked_in = True
    attendee.checked_in_at = datetime.utcnow()
    log = ScanLog(attendee_id=attendee.id, result="success", device_info="manual")
    db.add(log)
    db.commit()
    db.refresh(attendee)
    db.refresh(log)

    event = db.query(Event).filter(Event.id == attendee.event_id).first()
    if attendee.ghl_contact_id and event:
        try:
            await ghl_service.process_checkin_in_ghl(
                api_key=event.ghl_api_key or "",
                contact_id=attendee.ghl_contact_id,
                attended_tag=event.ghl_attended_tag,
                registered_tag=event.ghl_registered_tag,
            )
        except Exception as e:
            logger.error(f"GHL manual check-in update failed: {e}")

    await broadcaster.publish(attendee.event_id, {
        "type": "activity",
        "entry": _fmt_log(log, attendee),
        "stats": _get_stats(attendee.event_id, db),
    })
    return ScanResult(
        success=True,
        status="checked_in",
        message=f"✅ Manually checked in: {attendee.full_name}",
        attendee=attendee,
        is_vip=bool(attendee.is_vip),
    )


@router.post("/api/checkin/undo/{attendee_id}")
async def undo_checkin(
    attendee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Undo a check-in (for corrections)."""
    attendee = db.query(Attendee).filter(Attendee.id == attendee_id).first()
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")
    if not check_event_access(current_user, attendee.event_id, db):
        raise HTTPException(status_code=403, detail="You do not have permission for this event.")
    attendee.checked_in = False
    attendee.checked_in_at = None
    db.commit()
    return {"ok": True, "message": f"Check-in undone for {attendee.full_name}"}


@router.get("/api/events/{event_id}/checkin/recent")
async def recent_checkins(
    event_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Get recently checked-in attendees for the live dashboard."""
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied for this event.")
    attendees = (
        db.query(Attendee)
        .filter(Attendee.event_id == event_id, Attendee.checked_in == True)
        .order_by(Attendee.checked_in_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": a.id,
            "full_name": a.full_name,
            "email": a.email,
            "company": a.company,
            "ticket_id": a.ticket_id,
            "checked_in_at": a.checked_in_at.isoformat() if a.checked_in_at else None,
            "is_vip": bool(a.is_vip),
            "notes": a.notes,
        }
        for a in attendees
    ]


@router.get("/api/events/{event_id}/checkin/activity")
async def checkin_activity(
    event_id: int,
    limit: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Live activity feed — recent scans (success + duplicate) for the monitor screen."""
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")

    # Use outer join so not_found / wrong_event rows (no attendee) are also returned
    rows = (
        db.query(ScanLog, Attendee)
        .outerjoin(Attendee, ScanLog.attendee_id == Attendee.id)
        .filter(
            (ScanLog.event_id == event_id) |
            (Attendee.event_id == event_id)
        )
        .order_by(ScanLog.scanned_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": log.id,
            "attendee_id": attendee.id if attendee else None,
            "scanned_at": log.scanned_at.isoformat() if log.scanned_at else None,
            "result": log.result,
            "full_name": attendee.full_name if attendee else None,
            "email": attendee.email if attendee else None,
            "company": attendee.company if attendee else None,
            "is_vip": bool(attendee.is_vip) if attendee else False,
            "badge_issued": bool(attendee.badge_issued) if attendee else False,
            "profile_consent": bool(attendee.profile_consent) if attendee else False,
            "notes": attendee.notes if attendee else None,
            "scanned_ticket_id": log.scanned_ticket_id,
        }
        for log, attendee in rows
    ]


@router.delete("/api/checkin/scan-log/{log_id}")
async def delete_scan_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Delete an invalid scan log entry — used to dismiss errors on the monitor."""
    log = db.query(ScanLog).filter(ScanLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    if log.result not in ("not_found", "wrong_event"):
        raise HTTPException(status_code=400, detail="Only invalid scan logs can be deleted.")
    # Verify event access
    event_id = log.event_id or (log.attendee.event_id if log.attendee else None)
    if event_id and not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")
    db.delete(log)
    db.commit()
    if event_id:
        await broadcaster.publish(event_id, {"type": "log_deleted", "log_id": log_id})
    return {"ok": True}


@router.post("/api/attendees/{attendee_id}/badge-issued")
async def toggle_badge_issued(
    attendee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Toggle the badge_issued flag for an attendee — called from the check-in monitor."""
    attendee = db.query(Attendee).filter(Attendee.id == attendee_id).first()
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")
    if not check_event_access(current_user, attendee.event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")
    attendee.badge_issued = not attendee.badge_issued
    attendee.badge_issued_at = datetime.utcnow() if attendee.badge_issued else None
    db.commit()
    await broadcaster.publish(attendee.event_id, {
        "type": "badge_issued",
        "attendee_id": attendee.id,
        "badge_issued": bool(attendee.badge_issued),
        "stats": _get_stats(attendee.event_id, db),
    })
    return {"ok": True, "badge_issued": bool(attendee.badge_issued)}


@router.post("/api/attendees/{attendee_id}/profile-consent")
async def toggle_profile_consent(
    attendee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Toggle profile sharing consent for an attendee."""
    attendee = db.query(Attendee).filter(Attendee.id == attendee_id).first()
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")
    if not check_event_access(current_user, attendee.event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")
    attendee.profile_consent = not attendee.profile_consent
    db.commit()
    await broadcaster.publish(attendee.event_id, {
        "type": "profile_consent",
        "attendee_id": attendee.id,
        "profile_consent": bool(attendee.profile_consent),
    })
    return {"ok": True, "profile_consent": bool(attendee.profile_consent)}


@router.get("/checkout/{event_id}", response_class=HTMLResponse)
async def checkout_page(
    request: Request,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_login),
):
    """QR scanner for checking attendees out."""
    if isinstance(current_user, RedirectResponse):
        return current_user
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not check_event_access(current_user, event_id, db):
        return templates.TemplateResponse(
            "checkin/no_access.html",
            {"request": request, "event": event, "user": current_user},
            status_code=403,
        )
    checked_in = db.query(Attendee).filter(
        Attendee.event_id == event_id, Attendee.checked_in == True
    ).count()
    checked_out = db.query(Attendee).filter(
        Attendee.event_id == event_id, Attendee.checked_out == True
    ).count()
    return templates.TemplateResponse(
        "checkin/checkout.html",
        {
            "request": request,
            "event": event,
            "checked_in": checked_in,
            "checked_out": checked_out,
            "user": current_user,
        },
    )


@router.post("/api/checkin/checkout-scan", response_model=ScanResult)
async def checkout_scan(
    payload: ScanRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Process a QR code checkout scan."""
    if not check_event_access(current_user, payload.event_id, db):
        raise HTTPException(status_code=403, detail="You do not have permission for this event.")

    device_info = payload.device_info or request.headers.get("User-Agent", "")[:200]

    attendee = (
        db.query(Attendee)
        .filter(
            Attendee.ticket_id == payload.resolved_ticket_id,
            Attendee.event_id == payload.event_id,
        )
        .first()
    )

    if not attendee:
        wrong_event = db.query(Attendee).filter(Attendee.ticket_id == payload.resolved_ticket_id).first()
        if wrong_event:
            return ScanResult(
                success=False,
                status="wrong_event",
                message="❌ This ticket is for a different event.",
            )
        return ScanResult(
            success=False,
            status="not_found",
            message="❌ Ticket not found. Please check the QR code.",
        )

    if not attendee.checked_in:
        return ScanResult(
            success=False,
            status="not_checked_in",
            message=f"⚠️ {attendee.full_name} has not checked in yet.",
            attendee=attendee,
        )

    if attendee.checked_out:
        return ScanResult(
            success=False,
            status="duplicate",
            message=f"⚠️ {attendee.full_name} already checked out at {attendee.checked_out_at.strftime('%H:%M') if attendee.checked_out_at else 'earlier'}",
            attendee=attendee,
        )

    attendee.checked_out = True
    attendee.checked_out_at = datetime.utcnow()
    db.commit()
    db.refresh(attendee)

    event = db.query(Event).filter(Event.id == attendee.event_id).first()
    if attendee.ghl_contact_id and event and event.ghl_checkout_tag:
        try:
            await ghl_service.process_checkout_in_ghl(
                api_key=event.ghl_api_key or "",
                contact_id=attendee.ghl_contact_id,
                checkout_tag=event.ghl_checkout_tag,
            )
        except Exception as e:
            logger.error(f"GHL checkout update failed for {attendee.id}: {e}")

    await broadcaster.publish(attendee.event_id, {
        "type": "checkout",
        "attendee": {
            "id": attendee.id, "full_name": attendee.full_name,
            "email": attendee.email, "company": attendee.company,
            "is_vip": bool(attendee.is_vip),
            "checked_out_at": attendee.checked_out_at.isoformat() if attendee.checked_out_at else None,
        },
        "stats": _get_stats(attendee.event_id, db),
    })
    logger.info(f"✅ Checked out: {attendee.full_name} ({attendee.email})")
    return ScanResult(
        success=True,
        status="checked_out",
        message=f"✅ {attendee.first_name or attendee.full_name} checked out!",
        attendee=attendee,
        is_vip=bool(attendee.is_vip),
    )


@router.post("/api/checkin/manual-checkout")
async def manual_checkout(
    payload: ManualCheckIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Manually check out an attendee by ID."""
    attendee = db.query(Attendee).filter(Attendee.id == payload.attendee_id).first()
    if not attendee:
        raise HTTPException(status_code=404, detail="Attendee not found")
    if not check_event_access(current_user, attendee.event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")
    if not attendee.checked_in:
        return ScanResult(
            success=False,
            status="not_checked_in",
            message=f"{attendee.full_name} has not checked in yet",
            attendee=attendee,
        )
    if attendee.checked_out:
        return ScanResult(
            success=False,
            status="duplicate",
            message=f"{attendee.full_name} is already checked out",
            attendee=attendee,
        )
    attendee.checked_out = True
    attendee.checked_out_at = datetime.utcnow()
    db.commit()
    db.refresh(attendee)

    event = db.query(Event).filter(Event.id == attendee.event_id).first()
    if attendee.ghl_contact_id and event and event.ghl_checkout_tag:
        try:
            await ghl_service.process_checkout_in_ghl(
                api_key=event.ghl_api_key or "",
                contact_id=attendee.ghl_contact_id,
                checkout_tag=event.ghl_checkout_tag,
            )
        except Exception as e:
            logger.error(f"GHL manual checkout update failed: {e}")

    await broadcaster.publish(attendee.event_id, {
        "type": "checkout",
        "attendee": {
            "id": attendee.id, "full_name": attendee.full_name,
            "email": attendee.email, "company": attendee.company,
            "is_vip": bool(attendee.is_vip),
            "checked_out_at": attendee.checked_out_at.isoformat() if attendee.checked_out_at else None,
        },
        "stats": _get_stats(attendee.event_id, db),
    })
    return ScanResult(
        success=True,
        status="checked_out",
        message=f"✅ Manually checked out: {attendee.full_name}",
        attendee=attendee,
        is_vip=bool(attendee.is_vip),
    )


@router.get("/api/events/{event_id}/checkout/recent")
async def recent_checkouts(
    event_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_api),
):
    """Get recently checked-out attendees."""
    if not check_event_access(current_user, event_id, db):
        raise HTTPException(status_code=403, detail="Access denied.")
    attendees = (
        db.query(Attendee)
        .filter(Attendee.event_id == event_id, Attendee.checked_out == True)
        .order_by(Attendee.checked_out_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": a.id,
            "full_name": a.full_name,
            "email": a.email,
            "company": a.company,
            "checked_out_at": a.checked_out_at.isoformat() if a.checked_out_at else None,
            "is_vip": bool(a.is_vip),
        }
        for a in attendees
    ]


@router.get("/events/{event_id}/monitor", response_class=HTMLResponse)
async def monitor_page(
    request: Request,
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_login),
):
    """Live check-in monitor — designed for a second device at the door."""
    if isinstance(current_user, RedirectResponse):
        return current_user
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not check_event_access(current_user, event_id, db):
        return templates.TemplateResponse(
            "checkin/no_access.html",
            {"request": request, "event": event, "user": current_user},
            status_code=403,
        )
    return templates.TemplateResponse(
        "checkin/monitor.html",
        {"request": request, "event": event, "user": current_user},
    )
