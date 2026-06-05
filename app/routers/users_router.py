"""
User management (admin only) + check-in home page for check-in staff.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_password_hash, require_admin, require_admin_api, require_login
from app.database import get_db
from app.models import Event, EventPermission, Organisation, User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Check-in home (for check-in role users) ──────────────────

@router.get("/checkin-home", response_class=HTMLResponse)
async def checkin_home(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_login),
):
    """Landing page for check-in staff — shows only events they have permission for."""
    if isinstance(current_user, RedirectResponse):
        return current_user

    if current_user.is_developer:
        # Developer sees all live events across all orgs
        events = (
            db.query(Event)
            .filter(Event.is_archived == False)
            .order_by(Event.date.desc())
            .all()
        )
    elif current_user.is_org_admin:
        # Org admin sees all live events in their org
        events = (
            db.query(Event)
            .filter(Event.org_id == current_user.org_id, Event.is_archived == False)
            .order_by(Event.date.desc())
            .all()
        )
    else:
        # Check-in staff: only live events they have permission for, within their org
        permitted_ids = [
            p.event_id
            for p in db.query(EventPermission)
            .join(Event, Event.id == EventPermission.event_id)
            .filter(
                EventPermission.user_id == current_user.id,
                Event.org_id == current_user.org_id,
            )
            .all()
        ]
        events = (
            db.query(Event)
            .filter(Event.id.in_(permitted_ids), Event.is_archived == False)
            .order_by(Event.date.desc())
            .all()
        )

    return templates.TemplateResponse(
        "checkin/home.html",
        {"request": request, "events": events, "user": current_user},
    )


# ── User management pages (admin only) ───────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    if current_user.is_developer:
        users = db.query(User).order_by(User.created_at).all()
        orgs  = db.query(Organisation).order_by(Organisation.name).all()
    else:
        users = db.query(User).filter(User.org_id == current_user.org_id).order_by(User.created_at).all()
        orgs  = []
    return templates.TemplateResponse(
        "admin/users.html",
        {"request": request, "users": users, "orgs": orgs, "user": current_user},
    )


# ── User management API (admin only) ─────────────────────────

@router.post("/api/users")
async def create_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    display_name: str = Form(""),
    org_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # Determine allowed roles and org assignment
    if current_user.is_developer:
        if role not in ("developer", "admin", "checkin"):
            raise HTTPException(status_code=400, detail="Invalid role")
        try:
            target_org_id = int(org_id) if org_id and role in ("admin", "checkin") else None
        except ValueError:
            target_org_id = None
    elif current_user.is_org_admin:
        if role != "checkin":
            raise HTTPException(status_code=403, detail="Org admins can only create check-in users")
        target_org_id = current_user.org_id
    else:
        raise HTTPException(status_code=403, detail="Not permitted")

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Username '{username}' already exists")

    user = User(
        username=username,
        password_hash=get_password_hash(password),
        role=role,
        display_name=display_name or username,
        org_id=target_org_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "id": user.id, "username": user.username, "role": user.role}


@router.post("/api/users/{user_id}/password")
async def change_password(
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Org admin can only change passwords for users in their org
    if current_user.is_org_admin and user.org_id != current_user.org_id:
        raise HTTPException(status_code=403, detail="User not in your organisation")
    user.password_hash = get_password_hash(new_password)
    db.commit()
    return {"ok": True}


@router.post("/api/users/{user_id}/role")
async def change_role(
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    if current_user.is_developer:
        if role not in ("developer", "admin", "checkin"):
            raise HTTPException(status_code=400, detail="Invalid role")
    elif current_user.is_org_admin:
        if role not in ("admin", "checkin"):
            raise HTTPException(status_code=400, detail="Invalid role")
        if user.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail="User not in your organisation")
    else:
        raise HTTPException(status_code=403, detail="Not permitted")

    user.role = role
    db.commit()
    return {"ok": True}


@router.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_api),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if current_user.is_org_admin:
        if user.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail="User not in your organisation")
        if user.role != "checkin":
            raise HTTPException(status_code=403, detail="Org admins can only delete check-in users")
    db.delete(user)
    db.commit()
    return {"ok": True}
