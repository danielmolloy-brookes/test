"""
Organisation management — developer-only.
"""
import re
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import get_password_hash, require_developer, require_developer_api
from app.database import get_db
from app.models import Event, Organisation, User

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

SLUG_RE = re.compile(r'^[a-z0-9-]+$')


def _org_summary(org: Organisation, db: Session) -> dict:
    admin_count   = db.query(User).filter(User.org_id == org.id, User.role == "admin").count()
    checkin_count = db.query(User).filter(User.org_id == org.id, User.role == "checkin").count()
    event_count   = db.query(Event).filter(Event.org_id == org.id).count()
    return {
        "id":            org.id,
        "name":          org.name,
        "slug":          org.slug,
        "admin_count":   admin_count,
        "checkin_count": checkin_count,
        "event_count":   event_count,
    }


# ── Page routes ──────────────────────────────────────────────

@router.get("/admin/organisations", response_class=HTMLResponse)
async def organisations_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    orgs = db.query(Organisation).order_by(Organisation.name).all()
    orgs_data = [_org_summary(o, db) for o in orgs]
    return templates.TemplateResponse(
        "admin/organisations.html",
        {"request": request, "orgs": orgs_data, "user": current_user},
    )


@router.get("/admin/organisations/{org_id}", response_class=HTMLResponse)
async def organisation_detail_page(
    request: Request,
    org_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer),
):
    if isinstance(current_user, RedirectResponse):
        return current_user
    org = db.query(Organisation).filter(Organisation.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
    admins  = db.query(User).filter(User.org_id == org_id, User.role == "admin").order_by(User.display_name).all()
    checkin = db.query(User).filter(User.org_id == org_id, User.role == "checkin").order_by(User.display_name).all()
    events  = db.query(Event).filter(Event.org_id == org_id).order_by(Event.date.desc()).all()
    admins_json = [{"id": u.id, "username": u.username, "display_name": u.display_name or u.username} for u in admins]
    return templates.TemplateResponse(
        "admin/org_detail.html",
        {
            "request":       request,
            "org":           org,
            "admins":        admins,
            "admins_json":   admins_json,
            "checkin_users": checkin,
            "events":        events,
            "user":          current_user,
        },
    )


# ── API routes ───────────────────────────────────────────────

@router.post("/api/organisations")
async def create_organisation(
    name: str = Form(...),
    slug: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_api),
):
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Slug must contain only lowercase letters, numbers, and hyphens.")
    if db.query(Organisation).filter(Organisation.name == name.strip()).first():
        raise HTTPException(status_code=409, detail=f"An organisation named '{name}' already exists.")
    if db.query(Organisation).filter(Organisation.slug == slug).first():
        raise HTTPException(status_code=409, detail=f"The slug '{slug}' is already in use.")
    org = Organisation(name=name.strip(), slug=slug)
    db.add(org)
    db.commit()
    db.refresh(org)
    logger.info(f"Created organisation '{org.name}' (slug: {org.slug})")
    return {"ok": True, "id": org.id, "name": org.name, "slug": org.slug}


@router.delete("/api/organisations/{org_id}")
async def delete_organisation(
    org_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_api),
):
    org = db.query(Organisation).filter(Organisation.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
    user_count  = db.query(User).filter(User.org_id == org_id).count()
    event_count = db.query(Event).filter(Event.org_id == org_id).count()
    if user_count:
        raise HTTPException(status_code=400, detail=f"Cannot delete: organisation has {user_count} user(s). Reassign or delete them first.")
    if event_count:
        raise HTTPException(status_code=400, detail=f"Cannot delete: organisation has {event_count} event(s). Delete or reassign them first.")
    db.delete(org)
    db.commit()
    return {"ok": True}


@router.post("/api/organisations/{org_id}/admins")
async def create_org_admin(
    org_id: int,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_developer_api),
):
    """Create an org-admin user for the given organisation."""
    org = db.query(Organisation).filter(Organisation.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail=f"Username '{username}' already exists.")
    user = User(
        username=username,
        password_hash=get_password_hash(password),
        role="admin",
        display_name=display_name or username,
        org_id=org_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"Created org admin '{username}' for org '{org.name}'")
    return {"ok": True, "id": user.id, "username": user.username, "org": org.name}
